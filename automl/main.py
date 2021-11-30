
import site
site.addsitedir(r"D:\mytools\AI4Water")

import os
import gc
from typing import Union
from collections import OrderedDict

import pandas as pd
import numpy as np

from ai4water import Model
from ai4water._optimize import make_space
from ai4water.hyperopt import Categorical, HyperOpt, Integer
from ai4water.experiments.utils import regression_space
from ai4water.utils.utils import dateandtime_now
from ai4water.postprocessing.SeqMetrics import RegressionMetrics


SEP = os.sep


class OptimizePipeline(object):
    """
    optimizes model/estimator to use, its hyperparameters and preprocessing
    operation to be performed on features.

    Attributes:
        metrics


    """
    def __init__(
            self,
            data:pd.DataFrame,
            features,
            models: list = None,
            parent_iterations:int = 100,
            child_iterations:int = 25,
            parent_algorithm:str = "bayes",
            child_algorithm:str = "bayes",
            parent_val_metric:str = "mse",
            child_val_metric:str = "mse",
            parent_cross_validator:str = None,
            child_cross_validator:str = None,
            monitor:Union[list, str] = "r2",
            **model_kws
    ):
        """
        initializes

        Arguments:
            data:

            features:

            models:

            parent_iterations:

            child_iterations:

            parent_algorithm:

            child_algorithm:

            parent_val_metric:

            child_val_metric:

            parent_cross_validator:

            child_cross_validator:

            monitor:

            model_kws:
                any additional key word arguments for ai4water's Model

        """
        self.data = data
        self.features = features
        self.models = models
        self.parent_iters = parent_iterations
        self.child_iters = child_iterations
        self.parent_algo = parent_algorithm
        self.child_algo = child_algorithm
        self.parent_val_metric = parent_val_metric
        self.child_val_metric = child_val_metric
        self.parent_cv = parent_cross_validator
        self.child_cv = child_cross_validator
        self.model_kwargs = model_kws

        #self.seed = None

        if isinstance(monitor, str):
            monitor = [monitor]
        assert isinstance(monitor, list)

        self.metrics = {metric:OrderedDict() for metric in monitor}

        self.parent_suggestions = OrderedDict()

        self.parent_prefix = f"pipeline_opt_{dateandtime_now()}"

    @property
    def models(self):
        return self._models

    @models.setter
    def models(self, x):
        self._models = x

    def space(self):
        """makes the parameter space for parent hpo"""

        sp = make_space(self.features, categories=[
            "minmax", "zscore", "log", "robust", "quantile", "log2", "log10", "power", "none"])

        algos = Categorical(self.models,
            name="estimator")
        sp = sp + [algos]

        return sp

    def reset(self):

        self.parent_iter_ = 0
        self.child_iter_ = 0
        self.val_scores_ = OrderedDict()

        return

    def fit(self, previous_results=None):
        """

        Arguments:
            previous_results:
                path of file which contains xy values.
        """

        self.reset()

        parent_opt = HyperOpt(
            self.parent_algo,
            param_space=self.space(),
            objective_fn=self.parent_objective,
            num_iterations=self.parent_iters,
            opt_path=os.path.join(os.getcwd(), "results", self.parent_prefix)
        )

        if previous_results is not None:
            parent_opt.add_previous_results(previous_results)

        res = parent_opt.fit()

        setattr(self, 'optimizer', parent_opt)

        # make a 2d array of all erros being monitored.
        errors = np.column_stack([list(v.values()) for v in self.metrics.values()])
        # add val_scores as new columns
        errors = np.column_stack([errors, list(self.val_scores_.values())])
        # save the errors being monitored
        fpath = os.path.join(os.getcwd(), "results", self.parent_prefix, "errors.csv")
        pd.DataFrame(errors,
                     columns=list(self.metrics.keys()) + ['val_scores']
                     ).to_csv(fpath)

        return res

    def parent_objective(self, **suggestions)->float:
        """objective function for parent hpo loop"""

        self.parent_iter_ += 1

        #self.seed = np.random.randint(0, 10000, 1).item()

        # container for transformations for all features
        transformations = []

        for inp_feature, trans_method in suggestions.items():

            if inp_feature in self.data:
                if trans_method == "none":  # don't do anything with this feature
                    pass
                else:
                    # get the relevant transformation for this feature
                    t = {"method": trans_method, "features": [inp_feature]}

                    # some preprocessing is required for log based transformations
                    if trans_method.startswith("log"):
                        t["replace_nans"] = True
                        t["replace_zeros"] = True
                        t["treat_negatives"] = True

                    transformations.append(t)

        # optimize the hyperparas of estimator using child objective
        opt_paras = self.optimize_estimator_paras(
            suggestions['estimator'],
            transformations=transformations
        )

        self.parent_suggestions[self.parent_iter_] = {
            #'seed': self.seed,
            'transformation' :transformations,
            'estimator_paras': opt_paras
        }

        # fit the model with optimized hyperparameters and suggested transformations
        model = Model(
            model = {suggestions["estimator"]: opt_paras},
            data = self.data,
            val_metric = self.parent_val_metric,
            verbosity=0,
            #seed=self.seed,
            transformation=transformations,
            prefix=self.parent_prefix,
            **self.model_kwargs
        )

        if self.parent_cv is None:
            # train the model
            model.fit()

            # evaluate
            val_score = model.evaluate()
        else:
            val_score = model.cross_val_score()

        t,p = model.predict(return_true=True, process_results=False)
        errors = RegressionMetrics(t,p, remove_zero=True, remove_neg=True)

        for k,v in self.metrics.items():
            v[self.parent_iter_] = getattr(errors, k)()

        self.val_scores_[self.parent_iter_] = val_score

        formatter = "{:<5} {:<18.3f} " + "{:<15.7f} " * (len(self.metrics))
        print(formatter.format(
            self.parent_iter_,
            val_score,
            *[v[self.parent_iter_] for v in self.metrics.values()])
        )

        return val_score

    def optimize_estimator_paras(self, estimator:str, transformations:list)->dict:
        """optimizes hyperparameters of an estimator"""

        CHILD_PREFIX = f"{self.child_iter_}_{dateandtime_now()}"
        self.child_iter_ += 1

        def child_objective(**suggestions):
            """objective function for optimization of estimator parameters"""

            # build child model
            model = Model(
                model={estimator: suggestions},
                data=self.data,
                verbosity=0,
                val_metric=self.child_val_metric,
                transformation=transformations,
                #seed=self.seed,
                prefix=f"{self.parent_prefix}{SEP}{CHILD_PREFIX}",
                **self.model_kwargs
            )

            if self.child_cv is None:
                # fit child model
                model.fit()

                # evaluate child model
                val_score = model.evaluate(metrics=self.child_val_metric)
            else:
                val_score = model.cross_val_score()

            return val_score

        # make space
        child_space = regression_space(num_samples=10)[estimator]['param_space']

        optimizer = HyperOpt(
            self.child_algo,
            objective_fn=child_objective,
            num_iterations=self.child_iters,
            param_space=child_space,
            verbosity=0,
            process_results=False,
            opt_path = os.path.join(os.getcwd(), "results", self.parent_prefix, CHILD_PREFIX)
        )

        optimizer.fit()

        # free memory if possible
        gc.collect()

        # return the optimized parameters
        return optimizer.best_paras()
