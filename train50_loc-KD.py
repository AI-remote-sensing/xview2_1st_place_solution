import os
os.environ["MKL_NUM_THREADS"] = "1" 
os.environ["NUMEXPR_NUM_THREADS"] = "1" 
os.environ["OMP_NUM_THREADS"] = "1" 

from os import path, makedirs, listdir
import sys
import numpy as np
np.random.seed(1)
import random
random.seed(1)

import torch
from torch import nn
from torch.backends import cudnn
from torch.utils.data import Dataset
from torch.utils.data import DataLoader
import torch.optim.lr_scheduler as lr_scheduler

eps = 1e-6
from apex import amp

from adamw import AdamW
from losses import dice_round, ComboLoss

import pandas as pd
from tqdm import tqdm
import timeit
import cv2

from zoo.models import SeResNext50_Unet_Loc,SeResNext50_Unet_Loc_KD

from imgaug import augmenters as iaa

from utils import *

from sklearn.model_selection import train_test_split

from sklearn.metrics import accuracy_score

import gc

from apex import amp

cv2.setNumThreads(0)
cv2.ocl.setUseOpenCL(False)

train_dirs = ['train', 'tier3']

models_folder = 'weights'

input_shape = (512, 512)


all_files = []
for d in train_dirs:
    for f in sorted(listdir(path.join(d, 'images'))):
        if '_pre_disaster.png' in f:
            all_files.append(path.join(d, 'images', f))


class TrainData(Dataset):
    def __init__(self, train_idxs):
        super().__init__()
        self.train_idxs = train_idxs
        self.elastic = iaa.ElasticTransformation(alpha=(0.25, 1.2), sigma=0.2)

    def __len__(self):
        return len(self.train_idxs)

    def __getitem__(self, idx):
        _idx = self.train_idxs[idx]

        fn = all_files[_idx]

        img = cv2.imread(fn, cv2.IMREAD_COLOR)

        if random.random() > 0.985:
            img = cv2.imread(fn.replace('_pre_disaster', '_post_disaster'), cv2.IMREAD_COLOR)

        msk0 = cv2.imread(fn.replace('/images/', '/masks/'), cv2.IMREAD_UNCHANGED)

        if random.random() > 0.5:
            img = img[::-1, ...]
            msk0 = msk0[::-1, ...]

        if random.random() > 0.05:
            rot = random.randrange(4)
            if rot > 0:
                img = np.rot90(img, k=rot)
                msk0 = np.rot90(msk0, k=rot)

        if random.random() > 0.9:
            shift_pnt = (random.randint(-320, 320), random.randint(-320, 320))
            img = shift_image(img, shift_pnt)
            msk0 = shift_image(msk0, shift_pnt)
            
        if random.random() > 0.9:
            rot_pnt =  (img.shape[0] // 2 + random.randint(-320, 320), img.shape[1] // 2 + random.randint(-320, 320))
            scale = 0.9 + random.random() * 0.2
            angle = random.randint(0, 20) - 10
            if (angle != 0) or (scale != 1):
                img = rotate_image(img, angle, scale, rot_pnt)
                msk0 = rotate_image(msk0, angle, scale, rot_pnt)

        crop_size = input_shape[0]
        if random.random() > 0.3:
            crop_size = random.randint(int(input_shape[0] / 1.1), int(input_shape[0] / 0.9))

        bst_x0 = random.randint(0, img.shape[1] - crop_size)
        bst_y0 = random.randint(0, img.shape[0] - crop_size)
        bst_sc = -1
        try_cnt = random.randint(1, 5)
        for i in range(try_cnt):
            x0 = random.randint(0, img.shape[1] - crop_size)
            y0 = random.randint(0, img.shape[0] - crop_size)
            _sc = msk0[y0:y0+crop_size, x0:x0+crop_size].sum()
            if _sc > bst_sc:
                bst_sc = _sc
                bst_x0 = x0
                bst_y0 = y0
        x0 = bst_x0
        y0 = bst_y0
        img = img[y0:y0+crop_size, x0:x0+crop_size, :]
        msk0 = msk0[y0:y0+crop_size, x0:x0+crop_size]

        if crop_size != input_shape[0]:
            img = cv2.resize(img, input_shape, interpolation=cv2.INTER_LINEAR)
            msk0 = cv2.resize(msk0, input_shape, interpolation=cv2.INTER_LINEAR)

        if random.random() > 0.99:
            img = shift_channels(img, random.randint(-5, 5), random.randint(-5, 5), random.randint(-5, 5))

        if random.random() > 0.99:
            img = change_hsv(img, random.randint(-5, 5), random.randint(-5, 5), random.randint(-5, 5))

        if random.random() > 0.99:
            if random.random() > 0.99:
                img = clahe(img)
            elif random.random() > 0.99:
                img = gauss_noise(img)
            elif random.random() > 0.99:
                img = cv2.blur(img, (3, 3))
        elif random.random() > 0.99:
            if random.random() > 0.99:
                img = saturation(img, 0.9 + random.random() * 0.2)
            elif random.random() > 0.99:
                img = brightness(img, 0.9 + random.random() * 0.2)
            elif random.random() > 0.99:
                img = contrast(img, 0.9 + random.random() * 0.2)
                
        if random.random() > 0.999:
            el_det = self.elastic.to_deterministic()
            img = el_det.augment_image(img)

        msk = msk0[..., np.newaxis]

        msk = (msk > 127) * 1

        img = preprocess_inputs(img)

        img = torch.from_numpy(img.transpose((2, 0, 1))).float()
        msk = torch.from_numpy(msk.transpose((2, 0, 1))).long()

        sample = {'img': img, 'msk': msk, 'fn': fn}
        return sample


    
class ValData(Dataset):
    def __init__(self, image_idxs):
        super().__init__()
        self.image_idxs = image_idxs

    def __len__(self):
        return len(self.image_idxs)

    def __getitem__(self, idx):
        _idx = self.image_idxs[idx]

        fn = all_files[_idx]

        img = cv2.imread(fn, cv2.IMREAD_COLOR)

        msk0 = cv2.imread(fn.replace('/images/', '/masks/'), cv2.IMREAD_UNCHANGED)

        msk = msk0[..., np.newaxis]

        msk = (msk > 127) * 1

        img = preprocess_inputs(img)

        img = torch.from_numpy(img.transpose((2, 0, 1))).float()
        msk = torch.from_numpy(msk.transpose((2, 0, 1))).long()

        sample = {'img': img, 'msk': msk, 'fn': fn}
        return sample


def validate(model, data_loader):
    dices0 = []

    _thr = 0.5

    with torch.no_grad():
        for i, sample in enumerate(tqdm(data_loader)):
            msks = sample["msk"].numpy()
            imgs = sample["img"].cuda(non_blocking=True)
            
            out = model(imgs)

            msk_pred = torch.sigmoid(out[:, 0, ...]).cpu().numpy()
            
            for j in range(msks.shape[0]):
                dices0.append(dice(msks[j, 0], msk_pred[j] > _thr))

    d0 = np.mean(dices0)

    print("Val Dice: {}".format(d0))
    return d0


def evaluate_val_kd(data_val, best_score, model, snapshot_name, current_epoch):
    model.eval()
    d = validate(model, data_loader=data_val)

    if d > best_score:
        torch.save({
            'epoch': current_epoch + 1,
            'state_dict': model.state_dict(),
            'best_score': d,
        }, path.join(models_folder, snapshot_name + '_best'))
        best_score = d

    print("score: {}\tscore_best: {}".format(d, best_score))
    return best_score



def train_epoch_kd(current_epoch, seg_loss, model_s, model_t, optimizer, scheduler, train_data_loader,theta = 1,alpha = 1,beta = 1):
    losses = AverageMeter()

    dices = AverageMeter()

    iterator = tqdm(train_data_loader)
    model_s.train(mode=True)
    model_t.eval()
    for i, sample in enumerate(iterator):
        imgs = sample["img"].cuda(non_blocking=True)
        msks = sample["msk"].cuda(non_blocking=True)
        
        
        # with torch.no_grad():
        soft_out_t = model_t(imgs)
        soft_out_t = torch.sigmoid(soft_out_t[:,0, ...])
        feature_t = model_t.conv1(imgs)
        feature_t = model_t.conv2(feature_t)
        feature_t = model_t.conv3(feature_t)
        feature_t = model_t.conv4(feature_t)
        feature_t = model_t.conv5(feature_t)
            
        soft_out_s = model_s(imgs)
        soft_out_s = torch.sigmoid(soft_out_s[:,0, ...])
        loss_seg = seg_loss(soft_out_s, msks)
        feature_s = model_s.conv1(imgs)
        feature_s = model_s.conv2(feature_s)
        feature_s = model_s.conv3(feature_s)
        feature_s = model_s.conv4(feature_s)
        feature_s = model_s.conv5(feature_s)


        loss_cls = -torch.log(soft_out_s * msks + (1-soft_out_s) * (1-msks)).mean()
        loss_kf = torch.norm(feature_t-feature_s,p=2,dim=0).mean()
        loss_ko = -((soft_out_t * msks + (1-soft_out_t) * (1-msks))*torch.log(soft_out_s * msks + (1-soft_out_s) * (1-msks))).mean()
        loss = theta*loss_cls + loss_kf * alpha + loss_ko * beta +loss_seg

        with torch.no_grad():
            dice_sc = 1 - dice_round(soft_out_s, msks[:, 0, ...])
        losses.update(loss.item(), imgs.size(0))

        dices.update(dice_sc, imgs.size(0))
        iterator.set_description(
            "epoch: {}; lr {:.7f}; Loss {loss.val:.4f} ({loss.avg:.4f}),Loss_cls {loss_cls:.4f},Loss_kf {loss_kf:.4f},Loss_ko {loss_ko:.4f},Loss_seg {loss_seg:.4f}; Dice {dice.val:.4f} ({dice.avg:.4f})".format(
                current_epoch, scheduler.get_lr()[-1], loss=losses,loss_cls=theta * loss_cls.item(),loss_kf=alpha*loss_kf.item(),loss_ko=beta*loss_ko.item(),loss_seg = loss_seg.item(),dice=dices))
        
        optimizer.zero_grad()
        
        with amp.scale_loss(loss, optimizer) as scaled_loss:
            scaled_loss.backward()
        # loss.backward()
        torch.nn.utils.clip_grad_norm_(amp.master_params(optimizer), 1.1)
        optimizer.step()

    scheduler.step(current_epoch)

    print("epoch: {}; lr {:.7f}; Loss {loss.avg:.4f}".format(
                current_epoch, scheduler.get_lr()[-1], loss=losses))



if __name__ == '__main__':
    t0 = timeit.default_timer()

    makedirs(models_folder, exist_ok=True)
    
    seed = int(sys.argv[1]) 
    vis_dev = sys.argv[2]

    # os.environ['CUDA_DEVICE_ORDER'] = 'PCI_BUS_ID'
    os.environ["CUDA_VISIBLE_DEVICES"] = vis_dev

    cudnn.benchmark = True

    batch_size = 12
    val_batch_size = 4

    snapshot_name = 'res50_loc_{}_KD'.format(seed)

    train_idxs, val_idxs = train_test_split(np.arange(len(all_files)), test_size=0.1, random_state=seed)

    np.random.seed(seed+123)
    random.seed(seed+123)

    steps_per_epoch = len(train_idxs) // batch_size
    validation_steps = len(val_idxs) // val_batch_size

    print('steps_per_epoch', steps_per_epoch, 'validation_steps', validation_steps)

    data_train = TrainData(train_idxs)
    val_train = ValData(val_idxs)

    train_data_loader = DataLoader(data_train, batch_size=batch_size, num_workers=5, shuffle=True, pin_memory=False, drop_last=True)
    val_data_loader = DataLoader(val_train, batch_size=val_batch_size, num_workers=5, shuffle=False, pin_memory=False)

    model_s = SeResNext50_Unet_Loc_KD().cuda()
    model_t = SeResNext50_Unet_Loc().cuda()
    
    params = model_s.parameters()
    optimizer = AdamW(params, lr=0.00015, weight_decay=1e-6)
    model_s, optimizer = amp.initialize(model_s, optimizer, opt_level="O0")
    scheduler = lr_scheduler.MultiStepLR(optimizer, milestones=[4, 6,8,10,12,14,15,16,17,18,19,20], gamma=0.5)
    seg_loss = ComboLoss({'dice': 3.0, 'focal': 10.0}, per_image=False).cuda()
    
    
    checkpoint = torch.load('weights/res50_loc_0_tuned_best',map_location='cpu')
    loaded_dict = checkpoint['state_dict']
    sd = model_t.state_dict()
    for k in model_t.state_dict():
        if k in loaded_dict and sd[k].size() == loaded_dict[k].size():
            sd[k] = loaded_dict[k]
    loaded_dict = sd
    model_t.load_state_dict(loaded_dict)
    for key, value in model_t.named_parameters():# named_parameters()包含网络模块名称 key为模型模块名称 value为模型模块值，可以通过判断模块名称进行对应模块冻结
        value.requires_grad = False
    del loaded_dict
    del sd
    del checkpoint
    
    best_score = 0
    _cnt = -1
    torch.cuda.empty_cache()
    for epoch in range(20):
        train_epoch_kd(epoch, seg_loss, model_s, model_t, optimizer, scheduler, train_data_loader)
        if epoch % 1 == 0:
            _cnt += 1
            torch.cuda.empty_cache()
            best_score = evaluate_val_kd(val_data_loader, best_score, model_s, snapshot_name, epoch)

    elapsed = timeit.default_timer() - t0
    print('Time: {:.3f} min'.format(elapsed / 60))