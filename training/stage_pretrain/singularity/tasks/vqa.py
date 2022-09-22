import time
import datetime
import logging
import wandb
import os
from os.path import join

import torch
import torch.backends.cudnn as cudnn
import torch.distributed as dist

from models.model_vqa import Singularity

from utils.logger import log_dict_to_wandb, setup_wandb
from utils.config_utils import setup_main
from utils.basic_utils import MetricLogger, SmoothedValue, setup_seed, save_json
from utils.distributed import get_rank, get_world_size, is_main_process
from dataset.utils import sync_save_result
from dataset import create_dataset, create_sampler, create_loader, vqa_collate_fn
from tasks.vqa_utils import eval_qa_acc
from tasks.shared_utils import setup_model
from shutil import copyfile
from omegaconf import OmegaConf
import copy


logger = logging.getLogger(__name__)


def train(model, train_loader, optimizer, tokenizer, epoch, global_step,
          device, scheduler, scaler, config):
    model.train()

    metric_logger = MetricLogger(delimiter="  ")
    metric_logger.add_meter("lr", SmoothedValue(window=1, fmt="{value:.6f}"))
    metric_logger.add_meter("loss", SmoothedValue(window=1, fmt="{value:.4f}"))

    header = "Train Epoch: [{}]".format(epoch)
    log_freq = config.log_freq

    if config.distributed:
        train_loader.sampler.set_epoch(epoch)

    iterator = metric_logger.log_every(train_loader, log_freq, header)
    for i, data in enumerate(iterator):
        image, question, answer, weights, n = data
        image = image.to(device, non_blocking=True)
        weights = weights.to(device, non_blocking=True)
        question_input = tokenizer(
            question, padding="max_length", truncation=True,
            max_length=config.max_q_len, return_tensors="pt"
        ).to(device)
        answer_input = tokenizer(
            answer, padding="max_length", truncation=True,
            max_length=config.max_a_len, return_tensors="pt"
        ).to(device)

        with torch.cuda.amp.autocast(enabled=config.fp16):
            loss = model(
                image, question_input, answer_input,
                train=True, k=n, weights=weights
            )

        optimizer.zero_grad()
        scaler.scale(loss).backward()
        if config.optimizer.max_grad_norm > 0:
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(
                model.parameters(), config.optimizer.max_grad_norm)
        scaler.step(optimizer)
        scaler.update()
        scheduler.step()

        # logging
        metric_logger.update(loss=loss.item())
        metric_logger.update(lr=optimizer.param_groups[0]["lr"])

        if is_main_process() and config.wandb.enable \
                and global_step % log_freq == 0:
            logs = metric_logger.get_global_avg_dict()
            log_dict_to_wandb(logs, step=global_step, prefix="train/")

        global_step += 1

        if config.debug and (i+1) % 5 == 0:
            break

    metric_logger.synchronize_between_processes()
    logger.info(f"Averaged train stats: {metric_logger.global_avg()}")
    return global_step


@torch.no_grad()
def evaluation(model, data_loader, tokenizer, device, config):
    model.eval()

    metric_logger = MetricLogger(delimiter="  ")
    header = "[evaluation] Generating answers:"
    log_freq = config.log_freq // 2

    result = []

    raw_answer_list = data_loader.dataset.answer_list
    answer_list = [a + " " + config.eos for a in raw_answer_list]
    answer_input = tokenizer(
        answer_list, padding="max_length", truncation=True,
        max_length=config.max_a_len, return_tensors="pt"
    ).to(device)

    logger.info("Start generating results.")

    iterator = metric_logger.log_every(data_loader, log_freq, header)
    for n, data in enumerate(iterator):
        image, question, question_id = data
        image = image.to(device, non_blocking=True)
        question_input = tokenizer(
            question, padding="max_length", truncation=True,
            max_length=config.max_q_len, return_tensors="pt"
        ).to(device)

        topk_ids, topk_probs = model(
            image, question_input, answer_input, train=False, k=config.k_test)

        for ques_id, topk_id, topk_prob in zip(question_id, topk_ids, topk_probs):
            ques_id = int(ques_id.item()) if not isinstance(ques_id, str) else ques_id
            _, pred = topk_prob.max(dim=0)
            result.append({
                "question_id": ques_id, 
                "answer": raw_answer_list[topk_id[pred]]
            })

    return result


def setup_dataloaders(config, dataset_name="vqa"):
    logger.info(f"Creating {dataset_name} QA datasets")
    train_dataset = create_dataset("qa_train", config)
    test_datasets, test_dataset_names = create_dataset("qa_eval", config)
    all_datasets = [train_dataset] + test_datasets

    if config.distributed:
        num_tasks = get_world_size()
        global_rank = get_rank()
        samplers = create_sampler(
            all_datasets,
            shuffles=[True] + [False] * len(test_datasets),
            num_tasks=num_tasks,
            global_rank=global_rank
        )
    else:
        samplers = [None] * len(all_datasets)

    train_bsz = config.batch_size[train_dataset.media_type]
    test_bsz_list = [config.batch_size_test[d.media_type] for d in test_datasets]
    loaders = create_loader(
        all_datasets, samplers,
        batch_size=[train_bsz] + test_bsz_list,
        num_workers=[config.num_workers, ] * len(all_datasets),
        is_trains=[True] + [False] * len(test_datasets),
        collate_fns=[vqa_collate_fn] + [None] * len(test_datasets)
    )
    train_loader, test_loaders = loaders[0], loaders[1:]
    test_name2loaders = {k: v for k, v in zip(test_dataset_names, test_loaders)}
    return train_loader, test_name2loaders


def main(config):
    if is_main_process() and config.wandb.enable:
        run = setup_wandb(config)

    logger.info(f"config: \n{config}")
    logger.info(f"train_file: {config.train_file}")

    setup_seed(config.seed + get_rank())
    device = torch.device(config.device)
    cudnn.benchmark = True

    train_loader, test_name2loaders = setup_dataloaders(config)
    config.scheduler.num_training_steps = len(train_loader) * config.scheduler.epochs
    config.scheduler.num_warmup_steps = len(train_loader) * config.scheduler.warmup_epochs

    model, model_without_ddp, optimizer, scheduler, scaler, \
        tokenizer, start_epoch, global_step = setup_model(
            config,
            model_cls=Singularity,
            has_decoder=True,
            pretrain=False,
            find_unused_parameters=True
        )
    if is_main_process() and config.wandb.enable:
        wandb.watch(model)

    best = 0
    best_epoch = 0
    has_gt = config.dataset_name != "vqa" or config.test_types[0] == "minival"

    logger.info("Start " + "evaluation" if config.evaluate else "training")
    start_time = time.time()
    for epoch in range(start_epoch, config.scheduler.epochs):
        if not config.evaluate:
            global_step = train(
                model, train_loader, optimizer, tokenizer, epoch, global_step,
                device, scheduler, scaler, config
            )

        with torch.cuda.amp.autocast(enabled=config.fp16):
            eval_res = {}
            pred_name2file = {}
            for test_name, test_loader in test_name2loaders.items():
                if test_name not in config.test_types:
                    logger.info(f"Skip eval {test_name} split. All test_types {config.test_types}")
                    continue
                logger.info(f"Evaluating {test_name} split...")
                qa_result = evaluation(model, test_loader, tokenizer, device, config)

                # sync/gather results and eval
                pred_file, gathered_result = sync_save_result(
                    qa_result, config.result_dir, f"{test_name}_latest")
                pred_name2file[test_name] = pred_file
                if is_main_process() and has_gt:
                    eval_res[test_name] = eval_qa_acc(
                        test_loader.dataset.anno_list, gathered_result,
                        is_vqa=config.dataset_name == "vqa"
                    )

        if is_main_process():
            if len(eval_res) > 0:
                logger.info(f"eval_res {eval_res}")

                if config.wandb.enable:
                    for name, acc_dict in eval_res.items():
                        log_dict_to_wandb(acc_dict, step=global_step, prefix=f"{name}/")

            if not config.evaluate and has_gt and eval_res[config.stop_key]["overall"] > best:
                save_obj = {
                    "model": model_without_ddp.state_dict(),
                    "optimizer": optimizer.state_dict(),
                    "scheduler": scheduler.state_dict(),
                    "scaler": scaler.state_dict(),
                    "config": config,
                    "epoch": epoch,
                    "global_step": global_step,
                }
                for name, pred_file in pred_name2file.items():
                    copyfile(pred_file, pred_file.replace("latest", "best"))
                save_json(eval_res, join(config.output_dir, "eval_res_best.json"), save_pretty=True)
                torch.save(save_obj, join(config.output_dir, "ckpt_best.pth"))
                best = eval_res[config.stop_key]["overall"]
                best_epoch = epoch
            if config.evaluate:
                save_json(eval_res, join(config.output_dir, "eval_res_best.json"), save_pretty=True)

                for name, pred_file in pred_name2file.items():
                    copyfile(pred_file, pred_file.replace("latest", "eval"))
                    if has_gt:
                        save_path = join(config.output_dir, f"{name}_acc_eval.json")
                        save_json(eval_res[name], save_path, save_pretty=True)

        if config.evaluate or config.debug:
            break

        dist.barrier()

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    logger.info(f"Training time {total_time_str}")
    logger.info(f"best epoch {best_epoch}")
    logger.info(f"Checkpoints and Logs saved at {config.output_dir}")

    if is_main_process() and config.wandb.enable:
        run.finish()


def eval_after_training(train_config):
    # general config for all
    train_config.wandb.enable = False
    train_config.evaluate = True
    train_config.pretrained_path = join(train_config.output_dir, "ckpt_best.pth")

    eval_config = copy.deepcopy(train_config)
    eval_config.test_types = ["val", "test"] if eval_config.dataset_name != "vqa" else ["minival"]
    eval_config.output_dir = join(
        eval_config.output_dir, f"eval_after_training")
    eval_config.result_dir = eval_config.output_dir
    if is_main_process():
        os.makedirs(eval_config.output_dir, exist_ok=False)
        OmegaConf.save(eval_config, open(join(eval_config.output_dir, 'config.yaml'), 'w'))
    logger.info(f"===========> START eval_after_training [{eval_config.test_types}]")
    main(eval_config)


if __name__ == "__main__":
    cfg = setup_main()
    cfg.result_dir = cfg.output_dir
    main(cfg)
    if not cfg.evaluate:
        eval_after_training(cfg)
