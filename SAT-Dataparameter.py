import os
import argparse
import torch.optim as optim
from torchvision import transforms
import datetime
import numpy as np
from utils import Logger
import attack_generator as attack
from NoiLIn_utils.cifar import CIFAR10, CIFAR100
from NoiLIn_utils.svhn import SVHN
from NoiLIn_utils.utils import noisify
from models.resnet import *
from models.wrn_madry import *
from NoiLIn_utils.cifarIndex import CIFAR10WithIdx
import dataparameter


parser = argparse.ArgumentParser(description='PyTorch Adversarial Training with Automatic Noisy Labels Injection')
### Experimental setting ###
parser.add_argument('--gpu', type=str, default='0')
parser.add_argument('--seed', type=int, default=7, metavar='S', help='random seed')
parser.add_argument('--net', type=str, default="ResNet18",
                    help="decide which network to use,choose from resnet18,WRN_madry")
parser.add_argument('--depth', type=int, default=32, help='WRN depth')
parser.add_argument('--width_factor', type=int, default=10, help='WRN width factor')
parser.add_argument('--drop_rate', type=float, default=0.0, help='WRN drop rate')
parser.add_argument('--dataset', type=str, default="cifar10", help="choose from cifar10,svhn, cifar100", choices=['cifar10', 'cifar100', 'svhn'])
parser.add_argument('--out_dir', type=str, default='./AT_NoiLIn_', help='dir of output')
parser.add_argument('--data_dir', type=str, default='../data', help='the directory to access to dataset')
parser.add_argument('--resume', type=str, default='', help='whether to resume training, default: None')
### Training optimization setting ###
parser.add_argument('--epochs', type=int, default=200, metavar='N', help='number of epochs to train')
parser.add_argument('--optimizer', type=str, default='sgd')
parser.add_argument('--weight_decay', '--wd', default=5e-4, type=float, metavar='W')
parser.add_argument('--lr_max', type=float, default=0.1, metavar='LR', help='learning rate')
parser.add_argument('--momentum', type=float, default=0.9, metavar='M', help='SGD momentum')
parser.add_argument('--use_nat', type=int, help="use natural data and adversarial data together",default=0)
### Training attack setting ####
parser.add_argument('--rand_init', type=bool, default=True, help="whether to initialize adversarial sample with random noise")
parser.add_argument('--epsilon', type=float, default=0.031, help='perturbation bound')
parser.add_argument('--num_steps', type=int, default=10, help='maximum perturbation step K')
parser.add_argument('--step_size', type=float, default=0.007, help='step size')
### NoiLIn setting ###
parser.add_argument('--min_noise_rate', type=float, default=0.05)
parser.add_argument('--max_noise_rate', type=float, default=0.6)
parser.add_argument('--noise_type', type=str, default='symmetric',choices=['symmetric','pairflip','clean'])
parser.add_argument('--lr_schedule', type=str, default='piecewise')
parser.add_argument('--tau', type=int, help="sliding window size", default=10)
parser.add_argument('--gamma', type=float, help="boosting rate",default=0.05)


parser.add_argument('--learn_class_parameters', default=True, const=True, action='store_const',
                    help='Learn temperature per class')
parser.add_argument('--learn_inst_parameters', default=True, const=True, action='store_const',
                    help='Learn temperature per instance')
parser.add_argument('--skip_clamp_data_param', default=False, const=True, action='store_const',
                    help='Do not clamp data parameters during optimization')
parser.add_argument('--lr_class_param', default=0.1, type=float, help='Learning rate for class parameters')
parser.add_argument('--lr_inst_param', default=0.1, type=float, help='Learning rate for instance parameters')
parser.add_argument('--wd_class_param', default=0.0, type=float, help='Weight decay for class parameters')
parser.add_argument('--wd_inst_param', default=0.0, type=float, help='Weight decay for instance parameters')
parser.add_argument('--init_class_param', default=1.0, type=float, help='Initial value for class parameters')
parser.add_argument('--init_inst_param', default=1.0, type=float, help='Initial value for instance parameters')

args = parser.parse_args()
print(args)

# training settings
os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu
torch.manual_seed(args.seed)
np.random.seed(args.seed)
torch.cuda.manual_seed_all(args.seed)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False

def train(model, train_loader, optimizer, epoch, nr, optimizer_data_parameters, data_parameters, config):



    starttime = datetime.datetime.now()
    loss_sum = 0
    
     # Unpack data parameters
    optimizer_class_param, optimizer_inst_param = optimizer_data_parameters
    class_parameters, inst_parameters = data_parameters

    for batch_idx, (data, target, index_datasets) in enumerate(train_loader):
      

        # Flip a portion of data at each training minibatch
        if args.noise_type != 'clean':
            train_labels = np.asarray([[target[i]] for i in range(len(target))])
            noisy_labels, actual_noise_rate = noisify(train_labels=train_labels, noise_type=args.noise_type,
                                                            noise_rate=nr,
                                                            random_state=args.seed,
                                                            nb_classes=num_classes)
            noisy_labels = torch.Tensor([i[0] for i in noisy_labels]).long().squeeze()
            data, noisy_labels = data.cuda(), noisy_labels.cuda()
        else:
            data, noisy_labels = data.cuda(), target.cuda()

        # Get Most adversarial training data via PGD
        x_adv = attack.pgd(model,data,noisy_labels,epsilon=args.epsilon,step_size=args.step_size,num_steps= args.num_steps,loss_fn='cent',category='Madry',rand_init=True)

        model.train()
        lr = lr_schedule(epoch + 1)
        optimizer.param_groups[0].update(lr=lr)
           # Flush the gradient buffer for model and data-parameters
        optimizer.zero_grad()
        if args.learn_class_parameters:
            optimizer_class_param.zero_grad()
        if args.learn_inst_parameters:
            optimizer_inst_param.zero_grad()
        output = model(x_adv)

        if args.learn_class_parameters or args.learn_inst_parameters:
            # Compute data parameters for instances in the minibatch
            class_parameter_minibatch = class_parameters[target]

            #改index dataset
            inst_parameter_minibatch = inst_parameters[index_datasets]
            data_parameter_minibatch = dataparameter.get_data_param_for_minibatch(
                                            args,
                                            class_param_minibatch=class_parameter_minibatch,
                                            inst_param_minibatch=inst_parameter_minibatch)

            # Compute logits scaled by data parameters
            output = output / data_parameter_minibatch

        loss = nn.CrossEntropyLoss(reduction='mean')(output, noisy_labels)

        # Apply weight decay on data parameters
        if args.learn_class_parameters or args.learn_inst_parameters:
            loss = dataparameter.apply_weight_decay_data_parameters(args, loss,
                                                            class_parameter_minibatch=class_parameter_minibatch,
                                                            inst_parameter_minibatch=inst_parameter_minibatch)

        # if args.use_nat:
        #     nat_output = model(data)
        #     loss += nn.CrossEntropyLoss(reduction='mean')(nat_output, noisy_labels)
        #     loss /= 2
        loss_sum += loss.item()
        loss.backward()
        optimizer.step()
        if args.learn_class_parameters:
            optimizer_class_param.step()
        if args.learn_inst_parameters:
            optimizer_inst_param.step()

          # Clamp class and instance level parameters within certain bounds
        if args.learn_class_parameters or args.learn_inst_parameters:
            dataparameter.clamp_data_parameters(args, class_parameters, config, inst_parameters)


    # noise rate schedule
    endtime = datetime.datetime.now()
    time = (endtime - starttime).seconds
    return time, loss_sum


# config
config = {}
config['clamp_inst_sigma'] = {}
config['clamp_inst_sigma']['min'] = np.log(1/20)
config['clamp_inst_sigma']['max'] = np.log(20)
config['clamp_cls_sigma'] = {}
config['clamp_cls_sigma']['min'] = np.log(1/20)
config['clamp_cls_sigma']['max'] = np.log(20)
#dataparameter.save_config(args.save_dir, config)
if __name__ == '__main__': 
    # Learning schedules
    if args.lr_schedule == 'superconverge':
        lr_schedule = lambda t: np.interp([t], [0, args.epochs * 2 // 5, args.epochs], [0, args.lr_max, 0])[0]
    elif args.lr_schedule == 'piecewise':
        def lr_schedule(t):
            if t / args.epochs < 0.5:
                return args.lr_max
            elif t / args.epochs < 0.75:
                return args.lr_max / 10.
            else:
                return args.lr_max / 100.
    elif args.lr_schedule == 'linear':
        lr_schedule = lambda t: np.interp([t], [0, args.epochs // 3, args.epochs * 2 // 3, args.epochs], [args.lr_max, args.lr_max, args.lr_max / 10, args.lr_max / 100])[0]
    elif args.lr_schedule == 'onedrop':
        def lr_schedule(t):
            if t < args.lr_drop_epoch:
                return args.lr_max
            else:
                return args.lr_one_drop
    elif args.lr_schedule == 'multipledecay':
        def lr_schedule(t):
            return args.lr_max - (t//(args.epochs//10))*(args.lr_max/10)
    elif args.lr_schedule == 'cosine':
        def lr_schedule(t):
            return args.lr_max * 0.5 * (1 + np.cos(t / args.epochs * np.pi))

    # setup data loader
    transform_train = transforms.Compose([
        transforms.RandomCrop(32, padding=4),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
    ])
    transform_test = transforms.Compose([
        transforms.ToTensor(),
    ])

    print('==> Load Data')
    if args.dataset == "cifar10":
        train_dataset = CIFAR10WithIdx(root=args.data_dir,download=True,train=True,transform=transform_train,valid=False,valid_ratio=0.02)
        valid_dataset = CIFAR10WithIdx(root=args.data_dir, download=True, train=False, transform=transform_test, valid=True,valid_ratio=0.02)
        test_dataset = CIFAR10WithIdx(root=args.data_dir, download=True, train=False, transform=transform_test, valid=False,valid_ratio=0.02)
        num_classes = 10
    if args.dataset == "svhn":
        train_dataset = SVHN(root=args.data_dir, split='train', download=True, transform=transform_train,valid=False,valid_ratio=0.02)
        valid_dataset = SVHN(root=args.data_dir, split='train', download=True, transform=transform_test,valid=True,valid_ratio=0.02)
        test_dataset = SVHN(root=args.data_dir, split='test', download=True, transform=transform_test,valid=False,valid_ratio=0.02)
        args.step_size = 0.003
        args.lr_max = 0.01
        num_classes = 10
    if args.dataset == "cifar100":
        train_dataset = CIFAR100(root=args.data_dir,download=True,train=True,transform=transform_train,valid=False,valid_ratio=0.02)
        valid_dataset = CIFAR100(root=args.data_dir, download=True, train=False, transform=transform_test, valid=True,valid_ratio=0.02)
        test_dataset = CIFAR100(root=args.data_dir, download=True, train=False, transform=transform_test, valid=False,valid_ratio=0.02)
        num_classes = 100
    train_loader = torch.utils.data.DataLoader(train_dataset, batch_size=128, shuffle=True, num_workers=2)
    valid_loader = torch.utils.data.DataLoader(valid_dataset, batch_size=128, shuffle=False, num_workers=2)
    test_loader = torch.utils.data.DataLoader(test_dataset, batch_size=128, shuffle=False, num_workers=2)

    print('==> Load Model')
    if args.net == "ResNet18":
        model = ResNet18(num_classes).cuda()
        net = args.net
    if args.net == 'WRN_madry':
        model = Wide_ResNet_Madry(depth=args.depth, num_classes=10, widen_factor=args.width_factor, dropRate=args.drop_rate).cuda()
        net = "WRN_madry{}-{}-dropout{}".format(args.depth, args.width_factor, args.drop_rate)
    if len(args.gpu.split(',')) > 1:
        model = torch.nn.DataParallel(model)
    print(net)


    if args.noise_type == 'pairflip' or args.noise_type == 'symmetric':
        out_dir = args.out_dir + '{}_{}_{}_eps{}_{}_ratemin{}max{}_tau{}_gamma{}_seed{}'.format(net,args.dataset,
                                                                                                args.lr_schedule,
                                                                                                args.epsilon,
                                                                                                args.noise_type,
                                                                                                args.min_noise_rate,
                                                                                                args.max_noise_rate,
                                                                                                args.tau,
                                                                                                args.gamma,
                                                                                                args.seed)
    else:
        out_dir = args.out_dir + '{}_{}_{}_eps{}_clean_seed{}'.format(net, args.dataset,args.lr_schedule,args.epsilon,args.seed)

    if args.use_nat:
        out_dir += '_use_nat'
        
    print(out_dir)
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)

    def save_checkpoint(state, checkpoint=out_dir, filename='checkpoint.pth.tar'):
        filepath = os.path.join(checkpoint, filename)
        torch.save(state, filepath)

    def save_best_checkpoint(state, checkpoint=out_dir, filename='best_checkpoint.pth.tar'):
        filepath = os.path.join(checkpoint, filename)
        try:
            load_dict = torch.load(filepath)
            if state['test_pgd10_acc'] > load_dict['test_pgd10_acc']:
                torch.save(state, filepath)
        except:
            torch.save(state, filepath)

    optimizer = optim.SGD(model.parameters(), lr=args.lr_max, momentum=args.momentum, weight_decay=args.weight_decay)

    nr = args.min_noise_rate
    valid_acc_list = [0] * args.tau
    start_epoch = 0
    test_nat_acc = 0
    test_pgd10_acc = 0
    best_epoch = 0

    # Resume
    title = 'SAT-NoiLIn'
    if args.resume:
        # resume directly point to checkpoint.pth.tar e.g., --resume='./out-dir/checkpoint.pth.tar'
        print('==> SAT-NoiLIn Resuming from checkpoint ..')
        print(args.resume)
        assert os.path.isfile(args.resume)
        out_dir = os.path.dirname(args.resume)
        checkpoint = torch.load(args.resume)
        start_epoch = checkpoint['epoch']
        model.load_state_dict(checkpoint['state_dict'])
        valid_acc_list = checkpoint['valid_acc']
        nr = checkpoint['noise_rate']
        logger_test = Logger(os.path.join(out_dir, 'log_results.txt'), title=title, resume=True)
    else:
        print('==> STA-NoiLIn')
        logger_test = Logger(os.path.join(out_dir, 'log_results.txt'), title=title)
        logger_test.set_names(['Epoch', 'Natural Test Acc', 'PGD10 Acc', 'Noise_rate', 'Valid_acc'])

    (class_parameters, inst_parameters,
        optimizer_class_param, optimizer_inst_param) = dataparameter.get_class_inst_data_params_n_optimizer(
                                                            args=args,
                                                            nr_classes=num_classes,
                                                            nr_instances=len(train_loader.dataset),
                                                            device='cuda'
                                                            )

    for epoch in range(start_epoch, args.epochs):
        train_time, train_loss = train(model,
                                    train_loader, 
                                    optimizer, epoch, 
                                    nr, 
                                    optimizer_data_parameters=(optimizer_class_param, optimizer_inst_param),
                                    data_parameters=(class_parameters, inst_parameters),
                                    config=config)

        model.eval()
        loss, valid_pgd10_acc = attack.eval_robust(model, valid_loader, perturb_steps=args.num_steps, epsilon=args.epsilon,
                                                step_size=args.step_size, loss_fn="cent", category="Madry",
                                                rand_init=True)
        sum_before = np.sum(valid_acc_list)
        valid_acc_list[epoch % args.tau] = valid_pgd10_acc
        sum_after = np.sum(valid_acc_list)
        if sum_before > sum_after:
            nr *= (1 + args.gamma)
            if nr > args.max_noise_rate:
                nr = args.max_noise_rate

        if epoch <= 90:
            nr = min(args.max_noise_rate / 2, nr)

    
        loss, test_nat_acc = attack.eval_clean(model, test_loader)
        loss, test_pgd10_acc = attack.eval_robust(model, test_loader, perturb_steps=10, epsilon=args.epsilon,
                                                    step_size=args.step_size, loss_fn="cent", category="Madry",
                                                    rand_init=True)
        print(
            'Epoch: [%d | %d] | Train Time: %.2f s | Natural Test Acc %.4f | PGD10 Test Acc %.4f | Noise Rate %.4f | Valid Acc %.4f\n' % (
            epoch + 1,
            args.epochs,
            train_time,
            test_nat_acc,
            test_pgd10_acc,
            nr,
            valid_pgd10_acc)
            )

        logger_test.append([epoch + 1, test_nat_acc, test_pgd10_acc, nr, valid_pgd10_acc])


        save_best_checkpoint({
            'epoch': epoch + 1,
            'state_dict': model.state_dict(),
            'test_nat_acc': test_nat_acc,
            'test_pgd10_acc': test_pgd10_acc,
            'valid_acc': valid_acc_list,
            'noise_rate': nr,
        })
        
        save_checkpoint({
            'epoch': epoch + 1,
            'state_dict': model.state_dict(),
            'test_nat_acc': test_nat_acc,
            'test_pgd10_acc': test_pgd10_acc,
            'valid_acc': valid_acc_list,
            'noise_rate': nr,
        }, filename='checkpoint_epoch{}.pth.tar'.format(epoch + 1))