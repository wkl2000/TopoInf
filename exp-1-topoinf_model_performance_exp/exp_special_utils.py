import sys, os
import time
import copy
import json
import random
from typing import Union

import numpy as np
import networkx as nx

import torch
import torch_geometric
from torch_geometric.utils.convert import to_networkx

UPPER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(UPPER_DIR)
# from topoinf_impl import TopoInf
from topoinf_reg_impl import TopoInf
from base_utils.base_general_utils import fix_seed
from base_utils.base_io_utils import analyse_one_setting
from base_utils.base_training_utils import train, eval, print_eval_result, get_optimizer
from base_utils.model_2_filter import model_2_filter

sys.path.append(os.path.dirname(UPPER_DIR))


def RunExpWrapper(data, model, args, criterion, SEEDS):
    ### Train model ###
    test_acc_list = []
    for run_index in range(args.n_runs):
        seed = SEEDS[run_index]
        best_val_result = RunExp(data, model, args, criterion, run_index=run_index, seed=seed)
        test_acc_list.append(best_val_result['test_mask']['acc'])

    if args.skip_delete == True:
        setting_name = 'NO DELETE'
    else:
        delete_mag = args.delete_rate if args.delete_unit in ['mode_ratio', 'ratio'] \
                            else args.delete_num
        setting_name = f'{args.dataset.upper()} {args.model.upper()} ' \
                f'{args.delete_mode.upper()} {str(delete_mag)}'

    test_acc_mean, test_acc_uncertainty, test_acc_std = \
        analyse_one_setting(test_acc_list, setting_name = setting_name)

    analysed_result = {}
    analysed_result['test_acc_list'] = test_acc_list
    analysed_result['test_acc_mean'] = test_acc_mean
    analysed_result['test_acc_uncertainty'] = test_acc_uncertainty
    analysed_result['test_acc_std'] = test_acc_std

    return analysed_result


def RunExp(data, model, args, criterion, run_index=0, seed=2023, 
            save_file_suffix='',
            ):

    ## Training Preparation ##
    print('#'*30+f' [Run {run_index+1}/{args.n_runs}] '+'#'*30)

    ### Initial model and optimizer ###
    fix_seed(seed)
    model.reset_parameters()
    # optimizer = torch.optim.Adam(model.parameters(), weight_decay=args.weight_decay, lr=args.lr)
    optimizer = get_optimizer(model, args)

    eval_result = eval(model, data, criterion=None, get_detail=False)
    print_eval_result(eval_result, prefix='[Initial]')

    ## Start Training ##
    fix_seed(seed)
    
    best_val_acc = float('-inf')
    val_acc_history = []
    start_time = time.time()
    for epoch in range(1, 1+args.n_epochs):
        train(model, data, optimizer, criterion)

        if epoch % args.eval_interval == 0:
            eval_result = eval(model, data, criterion=None, get_detail=False)

            if epoch % (args.print_interval * args.eval_interval) == 0:
                print_eval_result(eval_result, prefix=f'[Epoch {epoch:3d}/{args.n_epochs:3d}]')
            
            if eval_result['val_mask']['acc'] > best_val_acc:
                best_val_acc = eval_result['val_mask']['acc']
                best_model_param = copy.deepcopy(model.state_dict())  # NOTE: `best_model_param` may be more efficient.

            val_acc_history.append(eval_result['val_mask']['acc'])
            if args.early_stopping > 0 and len(val_acc_history) > args.early_stopping:
                mean_val_acc = torch.tensor(
                    val_acc_history[-(args.early_stopping + 1):-1]).mean().item()
                if (eval_result['val_mask']['acc'] - mean_val_acc) * 100 < - args.early_stopping_tolerance: # NOTE: in percentage
                    print('[Early Stop Info] Stop at Epoch: ', epoch)
                    break
    
    train_time = time.time() - start_time

    ## Eval Best Result ##
    model.load_state_dict(best_model_param)
    best_eval_result_reduced = eval(model, data, criterion=None, get_detail=False)
    best_eval_result_reduced['train_time'] = train_time
    print_eval_result(best_eval_result_reduced, prefix=f'[Final Result] Time: {train_time:.2f}s |')

    ## Save Result ##
    if args.save_detailed_perf:
        best_eval_result_detailed = eval(model, data, criterion=criterion, get_detail=True)
        best_eval_result_detailed['train_time'] = train_time
        save_path_pt = os.path.join(args.save_dir,
                                    f"run_[{run_index+1}]_total_[{args.n_runs}]_[{save_file_suffix}].pt")
        torch.save(best_eval_result_detailed, save_path_pt)
        print(f"Save PT File: {save_path_pt}")

    if args.save_reduced_perf:
        save_path_json = os.path.join(args.save_dir, 
                                        f"run_[{run_index+1}]_total_[{args.n_runs}]_[{save_file_suffix}].json")
        with open(save_path_json, 'w') as f:
            json.dump(best_eval_result_reduced, f)
        print(f"Save JSON File: {save_path_json}")

    return best_eval_result_reduced


def compute_topoinf_wrapper(data, args):
    ### Define `topoinf_calculator` ###
    if 'coefficients' in args:
        coefficients = args.coefficients
    else:
        coefficients = model_2_filter(model_name=args.model, k_order=args.k_order)
    
    topoinf_calculator = TopoInf(data = data, 
            lambda_reg = args.lambda_reg,
            with_self_loops = not args.without_self_loops,
            k_order = args.k_order,
            coefficients = coefficients,
            distance_metric_name = args.distance_metric
            )
    
    if 'topoinf_node_masking' in args and args.topoinf_node_masking is not None and len(args.topoinf_node_masking) > 0:
        masking_nodes_indices = data[args.topoinf_node_masking[0]]
        for mask_name in args.topoinf_node_masking[1:]:
            masking_nodes_indices = masking_nodes_indices | data[mask_name]
        node_masking = set(torch.where(masking_nodes_indices)[0].tolist())
        print(f'[TOPOING INFO] #(node masking [{args.topoinf_node_masking}]): [{len(node_masking)}]')
    else:
        node_masking = None
        print('[TOPOING INFO] #(node masking): NONE')
    
    topoinf_calculator._pre_processing(node_masking = node_masking)
    ### Compute TopoInf ###
    topoinf_all_e = topoinf_calculator._compute_topoinf_edges_mp(_proc=8, verbose=True)

    return topoinf_all_e


def update_edge_index(G_data: Union[torch_geometric.data.Data, nx.graph.Graph], delete_edges: Union[list, tuple]):
    """Cut edges according to delete_edges.
    """
    if isinstance(G_data, nx.graph.Graph):
        G_networkx = G_data.copy()
        _device = 'cpu'
    elif isinstance(G_data, torch_geometric.data.Data):
        G_networkx = to_networkx(G_data, node_attrs=None, to_undirected=True, remove_self_loops=True)
        _device = G_data.edge_index.device
    else:
        raise NotImplementedError

    G_networkx.remove_edges_from(delete_edges)
    # updated edge_index
    edge_index = torch.tensor(np.array(G_networkx.to_directed().edges).T).to(_device)
    # NOTE: remember to turn Graph into directed and move edge_index to _device.

    return G_networkx, edge_index


def get_topoinf_wrapper(data, args):
    ### Get TopoInf Values ###
    if args.delete_strategy in ['topoinf', 'topoinf_random']:
        topoinf_all_e_dict = compute_topoinf_wrapper(data, args)
        # NOTE: use random number replace TopoInf
        if args.delete_strategy == 'topoinf_random':
            for edge, value in topoinf_all_e_dict.items():
                if value >= args.topoinf_threshold:     # Pos Edge
                    topoinf_all_e_dict[edge] = random.random() + args.topoinf_threshold     # NOTE: `random.random()` in [0, 1)
                elif value <= -args.topoinf_threshold:  # Neg Edge
                    topoinf_all_e_dict[edge] = - random.random() - args.topoinf_threshold
                else:   # -thr < value < thr
                    topoinf_all_e_dict[edge] = 0

    elif args.delete_strategy in ['all_random', 'label']:
        row, col = data.edge_index
        edge_tensor = data.edge_index[:, row < col]
        if args.delete_strategy == 'all_random':
            edge_list = edge_tensor.cpu().numpy().T.tolist()
            fake_topoinf_values = torch.rand(size=(len(edge_list), )) * 2 - 1   # NOTE: [0, 1] -> [-1, 1]
            fake_topoinf_values_list = fake_topoinf_values.tolist()
            edge_list = map(tuple, edge_list)   # NOTE: `list` is unhashable
            topoinf_all_e_dict = dict(zip(edge_list, fake_topoinf_values_list))
        elif args.delete_strategy == 'label':
            topoinf_all_e_dict = {}
            row, col = edge_tensor
            same_label_indices = (data.y[row] == data.y[col])
            
            same_label_edge_list = edge_tensor[:, same_label_indices].cpu().numpy().T.tolist()
            fake_topoinf_values_list = (- torch.rand(size=(len(same_label_edge_list), )) - args.topoinf_threshold).tolist()     # NOTE: [-1, 0)
            topoinf_all_e_dict.update(dict(zip(map(tuple, same_label_edge_list), fake_topoinf_values_list)))  # NOTE: `list` is unhashable

            diff_label_edge_list = edge_tensor[:, ~same_label_indices].cpu().numpy().T.tolist()
            fake_topoinf_values_list = (torch.rand(size=(len(diff_label_edge_list), )) + args.topoinf_threshold).tolist()     # NOTE: (0, 1]
            topoinf_all_e_dict.update(dict(zip(map(tuple, diff_label_edge_list), fake_topoinf_values_list)))  # NOTE: `list` is unhashable

    return topoinf_all_e_dict


def get_delete_edges_wrapper(topoinf_all_e_dict, args):
    ### Get Deleting Edges ###
    topoinf_all_e_sorted = sorted(topoinf_all_e_dict.items(), key=lambda item: item[1], reverse=True)
    topoinf_all_e_tensor = torch.tensor(list(topoinf_all_e_dict.values()))
    num_pos_edges = (topoinf_all_e_tensor > args.topoinf_threshold).sum().item()
    num_neg_edges = (topoinf_all_e_tensor < -args.topoinf_threshold).sum().item()
    print(f'[TOPOINF INFO] {num_pos_edges=} | {num_neg_edges=} | num_total_edges={len(topoinf_all_e_dict)}')

    if args.delete_unit == 'mode_ratio':
        if args.delete_mode == 'pos':
            delete_num = int(num_pos_edges*args.delete_rate)
        elif args.delete_mode == 'neg':
            delete_num = int(num_neg_edges*args.delete_rate)
    elif args.delete_unit == 'number':
        delete_num = args.delete_num
    elif args.delete_unit == 'ratio':
        num_edges = len(topoinf_all_e_sorted)
        delete_num = int(num_edges*args.delete_rate)

    if args.delete_mode == 'pos':
        delete_edges = [edge for edge, _ in topoinf_all_e_sorted[:delete_num]]
        print(f"[Info] delete [{delete_num}] edges out of [{num_pos_edges}] pos edges.")
        if delete_num > num_pos_edges:
            print(f"[Warning] num of del edges [{delete_num}] > num of pos edges [{num_pos_edges}]")
    elif args.delete_mode == 'neg':
        delete_edges = [edge for edge, _ in topoinf_all_e_sorted[-delete_num:]]
        print(f"[Info] delete [{delete_num}] edges out of [{num_neg_edges}] neg edges.")
        if delete_num > num_neg_edges:
            print(f"[Warning] num of del edges [{delete_num}] > num of neg edges [{num_neg_edges}]")

    print(f"Deleted {delete_num} {args.delete_mode.capitalize()} Edges.")

    delete_info = {
        'delete_num': delete_num,
        'ratio_in_total': delete_num / len(topoinf_all_e_tensor),
    }

    return delete_edges, delete_info


def topoinf_based_deleting_edges(data, topoinf_all_e, args):
    delete_edges, delete_info = get_delete_edges_wrapper(topoinf_all_e, args)
    _, edge_index = update_edge_index(data, delete_edges)

    return edge_index, delete_info