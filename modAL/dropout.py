import numpy as np
import sys
import torch 
from collections.abc import Mapping
from typing import Callable
import time

import logging
logger = logging.getLogger(__name__)

from sklearn.base import BaseEstimator
from sklearn.preprocessing import normalize

from scipy.special import entr

from modAL.utils.data import modALinput
from modAL.utils.selection import multi_argmax, shuffled_argmax

from skorch.utils import to_numpy

def default_logits_adaptor(input_tensor: torch.tensor, samples: modALinput): 
    # default Callable parameter for get_predictions
    return input_tensor

def KL_divergence(classifier: BaseEstimator, X: modALinput, n_instances: int = 1,
                random_tie_break: bool = False, dropout_layer_indexes: list = [], 
                num_cycles : int = 50, **mc_dropout_kwargs) -> np.ndarray:
    """
    TODO: Work in progress 
    """
    # set dropout layers to train mode
    set_dropout_mode(classifier.estimator.module_, dropout_layer_indexes, train_mode=True)

    predictions = get_predictions(classifier, X, num_cycles)

    # set dropout layers to eval
    set_dropout_mode(classifier.estimator.module_, dropout_layer_indexes, train_mode=False)

    #KL_divergence = _KL_divergence(predictions)
    
    if not random_tie_break:
        return multi_argmax(KL_divergence, n_instances=n_instances)

    return shuffled_argmax(KL_divergence, n_instances=n_instances)

def mc_dropout_multi(classifier: BaseEstimator, X: modALinput, query_strategies: list = ["bald", "mean_st", "max_entropy", "max_var"], 
                n_instances: int = 1, random_tie_break: bool = False, dropout_layer_indexes: list = [], 
                num_cycles : int = 50, sample_per_forward_pass: int = 1000,
                logits_adaptor: Callable[[torch.tensor, modALinput], torch.tensor] = default_logits_adaptor,
                **mc_dropout_kwargs) -> np.ndarray:
    """
    Multi metric dropout query strategy. Returns the specified metrics for given input data.
    Selection of query strategies are:
        - bald: BALD query strategy
        - mean_st: Mean Standard deviation
        - max_entropy: maximum entropy
        - max_var: maximum variation
    By default all query strategies are selected

    Function returns dictionary of metrics with their name as key.
    The indices of the n-best samples (n_instances) is not used in this function.
    """
    predictions = get_predictions(classifier, X, dropout_layer_indexes, num_cycles, sample_per_forward_pass, logits_adaptor)

    metrics_dict = {}
    if "bald" in query_strategies:
        metrics_dict["bald"] = _bald_divergence(predictions)
    if "mean_st" in query_strategies:
        metrics_dict["mean_st"] = _mean_standard_deviation(predictions)
    if "max_entropy" in query_strategies:
        metrics_dict["max_entropy"] = _entropy(predictions)
    if "max_var" in query_strategies:
        metrics_dict["max_var"] = _variation_ratios(predictions)

    return None, metrics_dict

def mc_dropout_bald(classifier: BaseEstimator, X: modALinput, n_instances: int = 1,
                random_tie_break: bool = False, dropout_layer_indexes: list = [], 
                num_cycles : int = 50, sample_per_forward_pass: int = 1000, 
                logits_adaptor: Callable[[torch.tensor, modALinput], torch.tensor] = default_logits_adaptor,
                **mc_dropout_kwargs,) -> np.ndarray:
    """
        Mc-Dropout bald query strategy. Returns the indexes of the instances with the largest BALD 
        (Bayesian Active Learning by Disagreement) score calculated through the dropout cycles
        and the corresponding bald score. 

        Based on the work of: 
            Deep Bayesian Active Learning with Image Data.
            (Yarin Gal, Riashat Islam, and Zoubin Ghahramani. 2017.)
            Dropout as a Bayesian Approximation: Representing Model Uncer- tainty in Deep Learning.
            (Yarin Gal and Zoubin Ghahramani. 2016.)
            Bayesian Active Learning for Classification and Preference Learning.
            (NeilHoulsby,FerencHusza ́r,ZoubinGhahramani,andMa ́te ́Lengyel. 2011.) 

        Args:
            classifier: The classifier for which the labels are to be queried.
            X: The pool of samples to query from.
            n_instances: Number of samples to be queried.
            random_tie_break: If True, shuffles utility scores to randomize the order. This
                can be used to break the tie when the highest utility score is not unique.
            dropout_layer_indexes: Indexes of the dropout layers which should be activated
                Choose indices from : list(torch_model.modules())
            num_cycles: Number of forward passes with activated dropout
            sample_per_forward_pass: max. sample number for each forward pass. 
                The allocated RAM does mainly depend on this.
                Small number --> small RAM allocation
            logits_adaptor: Callable which can be used to adapt the output of a forward pass 
                to the required vector format for the vectorised metric functions 
            **uncertainty_measure_kwargs: Keyword arguments to be passed for the uncertainty
                measure function.

        Returns:
            The indices of the instances from X chosen to be labelled;
            The mc-dropout metric of the chosen instances; 
    """
    time_before_prediction = time.time()
    predictions_1, predictions_2 = get_predictions(classifier, X, dropout_layer_indexes, num_cycles, sample_per_forward_pass, logits_adaptor)
    logger.info("Time for full prediction: {}".format(time.time()- time_before_prediction))

    #calculate BALD (Bayesian active learning divergence))
    
    time_before_bald_calculation = time.time()
    bald_scores = (_bald_divergence(predictions_1) + _bald_divergence(predictions_2))/2
    logger.info("Time for bald calculation: {}".format(time.time()- time_before_bald_calculation))


    if not random_tie_break:
        return multi_argmax(bald_scores, n_instances=n_instances)

    return shuffled_argmax(bald_scores, n_instances=n_instances)

def mc_dropout_mean_st(classifier: BaseEstimator, X: modALinput, n_instances: int = 1,
                random_tie_break: bool = False, dropout_layer_indexes: list = [], 
                num_cycles : int = 50, sample_per_forward_pass: int = 1000,
                logits_adaptor: Callable[[torch.tensor, modALinput], torch.tensor] = default_logits_adaptor,
                **mc_dropout_kwargs) -> np.ndarray:
    """
        Mc-Dropout mean standard deviation query strategy. Returns the indexes of the instances 
        with the largest mean of the per class calculated standard deviations over multiple dropout cycles
        and the corresponding metric.

        Based on the equations of: 
            Deep Bayesian Active Learning with Image Data. 
            (Yarin Gal, Riashat Islam, and Zoubin Ghahramani. 2017.)

        Args:
            classifier: The classifier for which the labels are to be queried.
            X: The pool of samples to query from.
            n_instances: Number of samples to be queried.
            random_tie_break: If True, shuffles utility scores to randomize the order. This
                can be used to break the tie when the highest utility score is not unique.
            dropout_layer_indexes: Indexes of the dropout layers which should be activated
                Choose indices from : list(torch_model.modules())
            num_cycles: Number of forward passes with activated dropout
            sample_per_forward_pass: max. sample number for each forward pass. 
                The allocated RAM does mainly depend on this.
                Small number --> small RAM allocation
            logits_adaptor: Callable which can be used to adapt the output of a forward pass 
                to the required vector format for the vectorised metric functions 
            **uncertainty_measure_kwargs: Keyword arguments to be passed for the uncertainty
                measure function.

        Returns:
            The indices of the instances from X chosen to be labelled;
            The mc-dropout metric of the chosen instances; 
    """

    # set dropout layers to train mode
    predictions_1, predictions_2 = get_predictions(classifier, X, dropout_layer_indexes, num_cycles, sample_per_forward_pass, logits_adaptor)

    mean_standard_deviations = (_mean_standard_deviation(predictions_1) + _mean_standard_deviation(predictions_2))/2

    if not random_tie_break:
        return multi_argmax(mean_standard_deviations, n_instances=n_instances)

    return shuffled_argmax(mean_standard_deviations, n_instances=n_instances)

def mc_dropout_max_entropy(classifier: BaseEstimator, X: modALinput, n_instances: int = 1,
                random_tie_break: bool = False, dropout_layer_indexes: list = [], 
                num_cycles : int = 50, sample_per_forward_pass: int = 1000,
                logits_adaptor: Callable[[torch.tensor, modALinput], torch.tensor] = default_logits_adaptor,
                **mc_dropout_kwargs) -> np.ndarray:
    """
        Mc-Dropout maximum entropy query strategy. Returns the indexes of the instances 
        with the largest entropy of the per class calculated entropies over multiple dropout cycles
        and the corresponding metric.

        Based on the equations of: 
            Deep Bayesian Active Learning with Image Data. 
            (Yarin Gal, Riashat Islam, and Zoubin Ghahramani. 2017.)

        Args:
            classifier: The classifier for which the labels are to be queried.
            X: The pool of samples to query from.
            n_instances: Number of samples to be queried.
            random_tie_break: If True, shuffles utility scores to randomize the order. This
                can be used to break the tie when the highest utility score is not unique.
            dropout_layer_indexes: Indexes of the dropout layers which should be activated
                Choose indices from : list(torch_model.modules())
            num_cycles: Number of forward passes with activated dropout
            sample_per_forward_pass: max. sample number for each forward pass. 
                The allocated RAM does mainly depend on this.
                Small number --> small RAM allocation
            logits_adaptor: Callable which can be used to adapt the output of a forward pass 
                to the required vector format for the vectorised metric functions 
            **uncertainty_measure_kwargs: Keyword arguments to be passed for the uncertainty
                measure function.

        Returns:
            The indices of the instances from X chosen to be labelled;
            The mc-dropout metric of the chosen instances; 
    """
    predictions_1, predictions_2 = get_predictions(classifier, X, dropout_layer_indexes, num_cycles, sample_per_forward_pass, logits_adaptor)

    #get entropy values for predictions
    entropy = (_entropy(predictions_1) + _entropy(predictions_2))/2

    if not random_tie_break:
        return multi_argmax(entropy, n_instances=n_instances)

    return shuffled_argmax(entropy, n_instances=n_instances)

def mc_dropout_max_variationRatios(classifier: BaseEstimator, X: modALinput, n_instances: int = 1,
                random_tie_break: bool = False, dropout_layer_indexes: list = [], 
                num_cycles : int = 50, sample_per_forward_pass: int = 1000,
                logits_adaptor: Callable[[torch.tensor, modALinput], torch.tensor] = default_logits_adaptor,
                **mc_dropout_kwargs) -> np.ndarray:
    """
        Mc-Dropout maximum variation ratios query strategy. Returns the indexes of the instances 
        with the largest variation ratios over multiple dropout cycles
        and the corresponding metric.

        Based on the equations of: 
            Deep Bayesian Active Learning with Image Data. 
            (Yarin Gal, Riashat Islam, and Zoubin Ghahramani. 2017.)

        Args:
            classifier: The classifier for which the labels are to be queried.
            X: The pool of samples to query from.
            n_instances: Number of samples to be queried.
            random_tie_break: If True, shuffles utility scores to randomize the order. This
                can be used to break the tie when the highest utility score is not unique.
            dropout_layer_indexes: Indexes of the dropout layers which should be activated
                Choose indices from : list(torch_model.modules())
            num_cycles: Number of forward passes with activated dropout
            sample_per_forward_pass: max. sample number for each forward pass. 
                The allocated RAM does mainly depend on this.
                Small number --> small RAM allocation
            logits_adaptor: Callable which can be used to adapt the output of a forward pass 
                to the required vector format for the vectorised metric functions 
            **uncertainty_measure_kwargs: Keyword arguments to be passed for the uncertainty
                measure function.

        Returns:
            The indices of the instances from X chosen to be labelled;
            The mc-dropout metric of the chosen instances; 
    """
    predictions_1, predictions_2 = get_predictions(classifier, X, dropout_layer_indexes, num_cycles, sample_per_forward_pass, logits_adaptor)

    #get variation ratios values for predictions
    variationRatios = (_variation_ratios(predictions_1) + _variation_ratios(predictions_2))/2 

    if not random_tie_break:
        return multi_argmax(variationRatios, n_instances=n_instances)

    return shuffled_argmax(variationRatios, n_instances=n_instances)

def get_predictions(classifier: BaseEstimator, X: modALinput, dropout_layer_indexes: list,
                num_predictions: int = 50, sample_per_forward_pass: int = 1000,
                logits_adaptor: Callable[[torch.tensor, modALinput], torch.tensor] = default_logits_adaptor):
    """
        Runs num_predictions times the prediction of the classifier on the input X 
        and puts the predictions in a list.

        Args:
            classifier: The classifier for which the labels are to be queried.
            X: The pool of samples to query from.
            dropout_layer_indexes: Indexes of the dropout layers which should be activated
                Choose indices from : list(torch_model.modules())
            num_predictions: Number of predictions which should be made
            sample_per_forward_pass: max. sample number for each forward pass. 
                The allocated RAM does mainly depend on this.
                Small number --> small RAM allocation
            logits_adaptor: Callable which can be used to adapt the output of a forward pass 
                to the required vector format for the vectorised metric functions 
        Return: 
            prediction: list with all predictions
    """
    
    predictions_1 = []
    predictions_2 = []

    # set dropout layers to train mode
    set_dropout_mode(classifier.estimator.module_, dropout_layer_indexes, train_mode=True)

    split_args = []

    number_of_samples = 0


    time_before_data_splitting = time.time()
    if isinstance(X, Mapping): #check for dict
        for k, v in X.items():
            number_of_samples = v.size(0)

            v.detach()
            split_v = torch.split(v, sample_per_forward_pass)
            #create sub-dictionary split for each forward pass with same keys&values
            for split_idx, split in enumerate(split_v):
                if len(split_args)<=split_idx:
                    split_args.append({})
                split_args[split_idx][k] = split
        
    elif torch.is_tensor(X): #check for tensor
        number_of_samples = X.size(0)
        X.detach()
        split_args = torch.split(X, sample_per_forward_pass)
    else:
        raise RuntimeError("Error in model data type, only dict or tensors supported")
    
    logger.info("Time for data splitting with {} samples: {}".format(sample_per_forward_pass, time.time()- time_before_data_splitting))


    with torch.no_grad(): 

        for i in range(num_predictions):

            probas_1 = None
            probas_2 = []

            """
            for index, samples in enumerate(split_args):
                #call Skorch infer function to perform model forward pass
                #In comparison to: predict(), predict_proba() the infer() 
                # does not change train/eval mode of other layers 
               
                logits = classifier.estimator.infer(samples)
                logger.info("Time for a single infer: {}".format(time.time()- time_before_infer))
                
                start_logits, end_logits = logits.transpose(1, 2).split(1, dim=1)
                start_logits = start_logits.squeeze(1).softmax(1)
                probas_1.append(start_logits)

                end_logits = end_logits.squeeze(1).softmax(1)
                probas_2.append(end_logits)
                logger.info("Time for a prediciton cycles with {} samples: {}".format(sample_per_forward_pass, time.time()- time_before_infer))
            """

            for samples in split_args:
                #call Skorch infer function to perform model forward pass
                #In comparison to: predict(), predict_proba() the infer() 
                # does not change train/eval mode of other layers 
                time_before_infer = time.time()
                logits = classifier.estimator.infer(samples)
                logger.info("Time for a single infer: {}".format(time.time()- time_before_infer))
                logger.info("logit_shape: {}".format(logits.shape))

                time_before_numpy_conversion = time.time()
                prediction = to_numpy(logits)
                logger.info("Time for numpy conversion: {}".format(time.time()-time_before_numpy_conversion))

                time_numpy_vstack = time.time()
                probas_1 = prediction if probas_1 is None else np.vstack((probas_1, prediction))
                logger.info("Time numpy_vstack {} samples: {}".format(sample_per_forward_pass, time.time()- time_numpy_vstack))
                logger.info("Time for a prediciton cycles with {} samples: {}".format(sample_per_forward_pass, time.time()- time_before_infer))
            
            predictions_1.append(probas_1)

            """
            probas_1 = torch.cat(probas_1)
            probas_2 = torch.cat(probas_2)

            predictions_1.append(to_numpy(probas_1))
            predictions_2.append(to_numpy(probas_2))
            """

    # set dropout layers to eval
    set_dropout_mode(classifier.estimator.module_, dropout_layer_indexes, train_mode=False)

    return predictions_1, predictions_1 #predictions_1, predictions_2

def entropy_sum(values: np.array, axis: int =-1):
    #sum Scipy basic entropy function: entr()
    entropy = entr(values)
    return np.sum(entropy, where=~np.isnan(entropy), axis=axis)

def _mean_standard_deviation(proba: list) -> np.ndarray: 
    """
        Calculates the mean of the per class calculated standard deviations.

        As it is explicitly formulated in: 
            Deep Bayesian Active Learning with Image Data. 
            (Yarin Gal, Riashat Islam, and Zoubin Ghahramani. 2017.)

        Args: 
            proba: list with the predictions over the dropout cycles
            mask: mask to detect the padded classes (must be of same shape as elements in proba)
        Return: 
            Returns the mean standard deviation of the dropout cycles over all classes. 
    """

    proba_stacked = np.stack(proba, axis=len(proba[0].shape)) 

    standard_deviation_class_vise = np.std(proba_stacked, axis=-1)
    mean_standard_deviation = np.mean(standard_deviation_class_vise, where=~np.isnan(standard_deviation_class_vise), axis=-1)

    return mean_standard_deviation

def _entropy(proba: list) -> np.ndarray: 
    """
        Calculates the entropy per class over dropout cycles

        As it is explicitly formulated in: 
            Deep Bayesian Active Learning with Image Data. 
            (Yarin Gal, Riashat Islam, and Zoubin Ghahramani. 2017.)

        Args: 
            proba: list with the predictions over the dropout cycles
            mask: mask to detect the padded classes (must be of same shape as elements in proba)
        Return: 
            Returns the entropy of the dropout cycles over all classes. 
    """

    proba_stacked = np.stack(proba, axis=len(proba[0].shape)) 

    #calculate entropy per class and sum along dropout cycles
    entropy_classes = entropy_sum(proba_stacked, axis=-1)
    entropy = np.mean(entropy_classes, where=~np.isnan(entropy_classes), axis=-1)
    return entropy

def _variation_ratios(proba: list) -> np.ndarray: 
    """
        Calculates the variation ratios over dropout cycles

        As it is explicitly formulated in: 
            Deep Bayesian Active Learning with Image Data. 
            (Yarin Gal, Riashat Islam, and Zoubin Ghahramani. 2017.)

        Args: 
            proba: list with the predictions over the dropout cycles
            mask: mask to detect the padded classes (must be of same shape as elements in proba)
        Return: 
            Returns the variation ratios of the dropout cycles. 
    """
    proba_stacked = np.stack(proba, axis=len(proba[0].shape)) 

    #Calculate the variation ratios over the mean of dropout cycles
    valuesDCMean = np.mean(proba_stacked, axis=-1)
    return 1 - np.amax(valuesDCMean, initial=0, where=~np.isnan(valuesDCMean), axis=-1)

def _bald_divergence(proba: list) -> np.ndarray:
    """
        Calculates the bald divergence for each instance

        As it is explicitly formulated in: 
            Deep Bayesian Active Learning with Image Data. 
            (Yarin Gal, Riashat Islam, and Zoubin Ghahramani. 2017.)

        Args: 
            proba: list with the predictions over the dropout cycles
            mask: mask to detect the padded classes (must be of same shape as elements in proba)
        Return: 
            Returns the mean standard deviation of the dropout cycles over all classes. 
    """
    proba_stacked = np.stack(proba, axis=len(proba[0].shape))

    #entropy along dropout cycles
    accumulated_entropy = entropy_sum(proba_stacked, axis=-1)
    f_x = accumulated_entropy/len(proba)

    #score sums along dropout cycles 
    accumulated_score = np.sum(proba_stacked, axis=-1)
    average_score = accumulated_score/len(proba)
    #expand dimension w/o data for entropy calculation
    average_score = np.expand_dims(average_score, axis=-1)

    #entropy over average prediction score 
    g_x = entropy_sum(average_score, axis=-1)

    #entropy differences
    diff = np.subtract(g_x, f_x)

    #sum all dimensions of diff besides first dim (instances) 
    shaped = np.reshape(diff, (diff.shape[0], -1))

    bald = np.sum(shaped, where=~np.isnan(shaped), axis=-1)
    return bald

def _KL_divergence(proba) -> np.ndarray:

    #create 3D or 4D array from prediction dim: (drop_cycles, proba.shape[0], proba.shape[1], opt:proba.shape[2])
    proba_stacked = np.stack(proba, axis=len(proba[0].shape))
    # TODO work in progress
    # TODO add dimensionality adaption
    #number_of_dimensions = proba_stacked.ndim
    #if proba_stacked.ndim > 2: 

    normalized_proba = normalize(proba_stacked, axis=0)


def set_dropout_mode(model, dropout_layer_indexes: list, train_mode: bool):
    """ 
        Function to enable the dropout layers by setting them to user specified mode (bool: train_mode)
        TODO: Reduce maybe complexity
        TODO: Keras support
    """

    modules = list(model.modules()) # list of all modules in the network.
    
    if len(dropout_layer_indexes) != 0:  
        for index in dropout_layer_indexes: 
            layer = modules[index]
            if layer.__class__.__name__.startswith('Dropout'): 
                if True == train_mode:
                    layer.train()
                elif False == train_mode:
                    layer.eval()
            else: 
                raise KeyError("The passed index: {} is not a Dropout layer".format(index))

    else: 
        for module in modules:
            if module.__class__.__name__.startswith('Dropout'):
                if True == train_mode:
                    module.train()
                elif False == train_mode:
                    module.eval()
