from validate import validate
from dataset.ourDataset import CustomDataset
from model.model_stage1 import TRIS
import torchvision
import CLIP.clip as clip
import torch.nn as nn
import numpy as np
import time
import torch
from mmengine.logging import MMLogger
import warnings
from scipy.sparse import SparseEfficiencyWarning
from tensorboardX import SummaryWriter
import datetime
from logger import create_logger
from utils.util import AverageMeter, load_checkpoint, save_checkpoint, load_pretrained_checkpoint
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler
from args import get_parser
from dataset.transform import get_transform
from torch.optim import AdamW
import torch.distributed as dist
import torch.nn.functional as F
import os
os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'
os.environ['CUDA_ENABLE_DEVICES'] = '0'


warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=SparseEfficiencyWarning)

# 设置日志级别为 WARNING
MMLogger.get_current_instance().setLevel('WARNING') 

device = "cuda" if torch.cuda.is_available() else "cpu"


def setup_seed(seed):
    import random
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.deterministic = True


setup_seed(3407)


def main(args):
    if args.distributed:
        local_rank = dist.get_rank()
        torch.cuda.set_device(local_rank)
    else:
        local_rank = 0

    model = TRIS(args)
    try:
        param_groups = model.trainable_parameters()
    except:
        print()
        param_groups = None
        print('no param goups...')
        print()
    if args.distributed:
        model.cuda(local_rank)
    else:
        model.cuda()

    if args.distributed:
        model = nn.SyncBatchNorm.convert_sync_batchnorm(model)
        model = torch.nn.parallel.DistributedDataParallel(
            model, device_ids=[local_rank], find_unused_parameters=True)
    else:
        model = torch.nn.DataParallel(model)

    model_without_ddp = model.module

    num_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    logger.info(f"number of params: {num_params / 1e6: .2f}M")
    # build dataset
    # train_dataset = ReferDataset(refer_data_root=args.refer_data_root,
    #                             dataset=args.dataset,
    #                             splitBy=args.splitBy,
    #                             bert_tokenizer=args.bert_tokenizer,
    #                             split='train',
    #                             size=args.size,
    #                             max_tokens=args.max_query_len,
    #                             image_transforms=get_transform(args.size,
    #                                                 train=True),
    #                             eval_mode=args.eval,
    #                             negative_samples=args.negative_samples,
    #                             positive_samples=args.positive_samples)

    ir_dir = "data/IVT_train/ir"
    vis_dir = "data/IVT_train/vi"
    text_dir = "data/IVT_train/text"

    train_dataset = CustomDataset(ir_dir=ir_dir, vis_dir=vis_dir, text_dir=text_dir, image_transforms=get_transform(
        args.size, train=True), max_tokens=args.max_query_len, bert_tokenizer=args.bert_tokenizer)
    val_datasets = []

    val_datasets.append(CustomDataset(ir_dir=ir_dir, vis_dir=vis_dir, text_dir=text_dir, image_transforms=get_transform(
        args.size, train=False), max_tokens=args.max_query_len, bert_tokenizer=args.bert_tokenizer))

    if args.distributed:
        train_sampler = DistributedSampler(train_dataset)
        val_samplers = []
        for val_dataset in val_datasets:
            val_samplers.append(DistributedSampler(val_dataset, shuffle=False))
    else:
        train_sampler = None
        val_samplers = []
        for val_dataset in val_datasets:
            val_samplers.append(None)

    train_loader = DataLoader(train_dataset,
                              batch_size=args.batch_size,
                              num_workers=2,
                              pin_memory=True,
                              sampler=train_sampler,
                              shuffle=(train_sampler is None))
    val_loaders = []

    for val_dataset, val_sampler in zip(val_datasets, val_samplers):
        val_loaders.append(DataLoader(val_dataset,
                                      batch_size=1,
                                      num_workers=2,
                                      pin_memory=True,
                                      sampler=val_sampler,
                                      shuffle=False))

    if param_groups is not None:
        print('param_groups is NOne !')
        optimizer = AdamW([
            {'params': param_groups[0], 'lr': args.lr *
                args.lr_multi, 'weight_decay': args.weight_decay},
            {'params': param_groups[1], 'lr': args.lr,
                'weight_decay': args.weight_decay},
        ], lr=args.lr, weight_decay=args.weight_decay)
    else:
        optimizer = AdamW(params=model.parameters(),
                          lr=args.lr,
                          weight_decay=args.weight_decay)
    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer,
                                                  lambda x: (1 - x / (len(train_loader) * args.epoch)) ** 0.9)

    if args.resume:
        if args.pretrain is not None:
            load_checkpoint(args, model_without_ddp,
                            optimizer, scheduler, logger)
        if args.eval:
            st = time.time()
            val_acc, testA_acc, testB_acc = 0, 0, 0
            for i, val_loader in enumerate(val_loaders):
                oIoU, mIoU, hit = validate(args, val_loader, model, local_rank)
                if i == 0:
                    val_acc = mIoU
                elif i == 1:
                    testA_acc = mIoU
                else:
                    testB_acc = mIoU
            print(f'val: {val_acc}, testA, {testA_acc}, testB: {testB_acc}')
            all_t = time.time() - st
            print(
                f'Testing time:  {str(datetime.timedelta(seconds=int(all_t)))}')
            return

    logger.info("\nStart training")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    tmp_max_len = args.max_query_len
    clip_model, _ = clip.load(
        "ViT-B/32", device=device, jit=False, txt_length=tmp_max_len)
    clip_model.eval()

    train_time = 0
    start_time = time.time()
    best = {
        'val_acc': -1,
        'val_hit': -1,
        'epoch': -1,
        'path': '',
        'hit': -1,
        'hit_path': '',
        'testA': -1,
        'testB': -1
    }
    iteration = 0
    for epoch in range(args.start_epoch, args.epoch):
        st = time.time()
        if args.distributed:
            train_loader.sampler.set_epoch(epoch)
        iteration = train_one_epoch(train_loader, model, optimizer, epoch,
                                    local_rank, args, iteration, clip_model, lr_scheduler=scheduler)

        train_time += time.time() - st
        save_path = save_checkpoint(epoch=epoch, model=model_without_ddp, optimizer=optimizer,
                                    scheduler=scheduler, logger=logger, args=args, checkpoint_name=f'ckpt_320_epoch_{epoch}_hit.pth')

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    logger.info('Training time {}'.format(train_time))
    logger.info('Training + testing time {}'.format(total_time_str))


def clip_forward(clip_model, images, tokenized_text):
    image_features = clip_model.encode_image(images)
    _, text_features = clip_model.encode_text(tokenized_text)

    # normalized features
    image_features = image_features / image_features.norm(dim=-1, keepdim=True)
    text_features = text_features / text_features.norm(dim=-1, keepdim=True)

    N, C = image_features.size()
    image_features = image_features.reshape(N, 1, C)
    N, C = text_features.size()
    text_features = text_features.reshape(N, C, 1)

    similarity = torch.matmul(image_features, text_features)

    return similarity


def MaxLoss(x):
    margin = 0
    weights = 1
    x = x.clamp(0.0001, 0.9999)
    return -(torch.log(x + margin) * weights).mean()


def train_one_epoch(train_loader, model, optimizer, epoch, local_rank, args, iteration=0, clip_model=None, lr_scheduler=None):
    num_steps = len(train_loader)
    model.train()

    batch_time = AverageMeter()
    loss_meter = AverageMeter()

    start = time.time()
    end = time.time()

    max_iter = int(num_steps * args.epoch)
    # print('='*20, ',  max_iter = ', max_iter)
    clip_input_size = 224
    l1, l2, l3, l4, l5 = torch.tensor(0), torch.tensor(
        0), torch.tensor(0), torch.tensor(0), torch.tensor(0)

    for idx, (samples, targets, image_path1, image_path2) in enumerate(train_loader):

        word_ids = samples['word_ids'].squeeze(1)  # 48 1 20 -》48 20
        img_1 = samples['img_ir'].cuda(local_rank, non_blocking=True)
        img_2 = samples['img_vis'].cuda(local_rank, non_blocking=True)
        target = targets['target'].cuda(local_rank, non_blocking=True)

        word_ids = word_ids.cuda(local_rank, non_blocking=True)  # 48 20

        B, c, h, w = img_1.shape

        raw_sentences = targets['sentences']

        labels = torch.eye(B).cuda()  # 同一批次的其他图像的描述都是负样本

        if args.mode != 'clip':
            cls, _, _, sig_out, _ = model(
                img_1, img_2, raw_sentences)  # ir vis
        else:
            cls, _, _, sig_out, _, regulation_loss_final = model(
                img_1, img_2, word_ids)

        if img_2.shape[2] != clip_input_size:
            cam_224 = F.interpolate(
                sig_out, (clip_input_size, clip_input_size), mode='bilinear', align_corners=True)
            img_224 = F.interpolate(img_2*0.35 + img_1*0.65, (clip_input_size,
                                    clip_input_size), mode='bilinear', align_corners=True)
        else:
            cam_224 = sig_out
            img_224 = img_2*0.35 + img_1*0.65
        fg_224_eval = []
        bg_224_eval = []
        for i in range(len(img_224)):
            fg_224_eval.append(cam_224[i] * img_224[i])
            bg_224_eval.append((1 - cam_224[i]) * img_224[i])
        fg_224_eval = torch.stack(fg_224_eval, dim=0)  # 分割的对象前景 跟 查询相关性计算
        bg_224_eval = torch.stack(bg_224_eval, dim=0)

        fg_loss = MaxLoss(clip_forward(clip_model, fg_224_eval, word_ids))

        if args.negative_samples > 0:  # 校准过程中的再优化
            neg_phrases = samples['neg_word_ids']

            image_features = clip_model.encode_image(fg_224_eval)
            cbs_loss = torch.tensor(.0, requires_grad=True, device='cuda:0')
            for i_ in range(B):
                _, text_features = clip_model.encode_text(
                    neg_phrases[i_].cuda())
                image_feature = image_features[i_].reshape(1, -1)
                image_feature = image_feature / \
                    image_feature.norm(dim=-1, keepdim=True)
                text_features = text_features / \
                    text_features.norm(dim=-1, keepdim=True)
                neg_score = torch.matmul(
                    image_feature, text_features.transpose(0, 1))  # 每个图像对应的三个样本
                cbs_loss = cbs_loss + \
                    (-(torch.log(1 - neg_score)).mean())  # 批次累计
            cbs_loss /= B  # 批次均值

        cls_loss = F.multilabel_soft_margin_loss(cls, labels)

        l1 = fg_loss
        l4 = cls_loss

        if args.negative_samples > 0:
            l5 = cbs_loss
        else:
            l5 = torch.tensor(0)

        loss = l1 * args.w1 + l2 * args.w2 + l3 * args.w3 + l4 * \
            args.w4 + l5 * args.w5 + regulation_loss_final * 5

        optimizer.zero_grad()

        loss.backward()

        optimizer.step()
        if lr_scheduler is not None:
            lr_scheduler.step()

        torch.cuda.synchronize()

        if local_rank == 0:
            lr = optimizer.param_groups[0]["lr"]
            writer.add_scalar("optim/lr", lr, iteration)
            writer.add_scalar("train/loss", loss.data.cpu().numpy(), iteration)
            writer.add_scalar("train/l1", l1.data.cpu().numpy(), iteration)
            writer.add_scalar("train/l2", l2.data.cpu().numpy(), iteration)
            writer.add_scalar("train/l3", l3.data.cpu().numpy(), iteration)
            writer.add_scalar("train/l4", l4.data.cpu().numpy(), iteration)
            writer.add_scalar("train/l5", l5.data.cpu().numpy(), iteration)
            writer.add_scalar(
                "train/l6", regulation_loss_final.data.cpu().numpy(), iteration
            )

        loss_meter.update(loss.item(), target.size(0))
        batch_time.update(time.time() - end)
        end = time.time()

        if idx % args.print_freq == 0 and local_rank == 0:
            lr = optimizer.param_groups[0]["lr"]
            memory_used = torch.cuda.max_memory_allocated() / (1024.0 * 1024.0)
            etas = batch_time.avg * (num_steps - idx)
            all_etas = batch_time.avg * (max_iter - iteration)
            logger.info(
                f"Train:[{epoch:2d}/{args.epoch}][{idx:4d}/{num_steps}] | "
                f"eta: {datetime.timedelta(seconds=int(etas))} | lr {lr:.6f} || "
                f"loss: {loss_meter.val:.4f} ({loss_meter.avg:.4f}) | "
                f"l1: {l1:.4f} | "
                f"l2: {l2:.4f} | "
                f"l3: {l3:.4f} | "
                f"l4: {l4:.4f} | "
                f"l5: {l5:.4f} | "
                f"l6: {regulation_loss_final:.4f} | "
                f"time: {batch_time.val:.4f} ({batch_time.avg:.4f}) | "
                f"mem: {memory_used:.0f}MB || "
                f"all_eta: {datetime.timedelta(seconds=int(all_etas))}"
            )
        iteration += 1
    epoch_time = time.time() - start
    logger.info(
        f"EPOCH {epoch} training takes {datetime.timedelta(seconds=int(epoch_time))}"
    )
    return iteration


if __name__ == "__main__":
    parse = get_parser()
    args = parse.parse_args()

    print('========='*10)
    print(args)
    print('========='*10)

    if args.vis_out is not None and not os.path.exists(args.vis_out):
        os.mkdir(args.vis_out)

    if 'RANK' in os.environ and 'WORLD_SIZE' in os.environ:
        rank = int(os.environ['RANK'])
        world_size = int(os.environ['WORLD_SIZE'])
        print(f"RANK and WORLD_SIZE in environ: {rank}/{world_size}")
    else:
        rank = -1
        world_size = -1

    if args.distributed:
        torch.distributed.init_process_group(
            backend='nccl', init_method='env://', world_size=world_size, rank=rank)
        torch.distributed.barrier()

    if args.distributed:
        logger = create_logger(dist_rank=dist.get_rank())
    else:
        logger = create_logger()

    global writer
    writer = SummaryWriter(args.board_folder)
    main(args)
