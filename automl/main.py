
import site
site.addsitedir('E:\\AA\\AI4Water')

import os
import gc
import math
from typing import Union, Dict
from collections import OrderedDict, defaultdict

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.lines as mlines

from ai4water import Model
from ai4water._optimize import make_space
from ai4water.hyperopt import Categorical, HyperOpt, Integer
from ai4water.hyperopt.utils import to_skopt_space
from ai4water.experiments.utils import regression_space, classification_space
from ai4water.utils.utils import dateandtime_now
from ai4water.postprocessing.SeqMetrics import RegressionMetrics, ClassificationMetrics
from ai4water.utils.utils import MATRIC_TYPES


SEP = os.sep

DEFAULT_TRANSFORMATIONS = [
    "minmax", "center", "scale", "zscore", "box-cox", "yeo-johnson",
    "quantile", "robust", "log", "log2", "log10", "sqrt", "none",
              ]
DEFAULT_Y_TRANFORMATIONS = ["log", "log2", "log10", "sqrt", "none"]


class OptimizePipeline(object):
    """
    optimizes model/estimator, its hyperparameters and preprocessing
    operation to be performed on input and output features. It consists of two
    hpo loops. The parent or outer loop optimizes preprocessing/feature engineering,
    feature selection and model selection while the child hpo loop optimizes
    hyperparmeters of child hpo loop.

    Attributes
    -----------

    - metrics

    - parent_suggestions:
        an ordered dictionary of suggestions to the parent objective function
        during parent hpo loop

    - child_val_metrics:
        a numpy array containing val_metrics of all child hpo loops

    - optimizer
        an instance of ai4water.hyperopt.HyperOpt for parent optimization

    - models
        a list of models being considered for optimization

    - estimator_space
        a dictionary which contains parameter space for each model

    Example:
        >>> from automl import OptimizePipeline
        >>> from ai4water.datasets import arg_beach
        >>> data = arg_beach()
        >>> input_features = data.columns.tolist()[0:-1]
        >>> output_features = data.columns.tolist()[-1:]
        >>> pl = OptimizePipeline(input_features=input_features,
        >>>                       output_features=output_features,
        >>>                       inputs_to_transform=input_features)
        >>> results = pl.fit(data=data)

    Note
    -----
    This optimizationa always sovlves a minimization problem even if the val_metric
    is r2.
    """

    def __init__(
            self,
            inputs_to_transform,
            input_transformations: Union[list, dict] = None,
            outputs_to_transform=None,
            output_transformations: Union[list, ] = None,
            models: list = None,
            parent_iterations: int = 100,
            child_iterations: int = 25,
            parent_algorithm: str = "bayes",
            child_algorithm: str = "bayes",
            parent_val_metric: str = "mse",
            child_val_metric: str = "mse",
            cv_parent_hpo: bool = None,
            cv_child_hpo: bool = None,
            monitor: Union[list, str] = "r2",
            mode: str = "regression",
            **model_kws
    ):
        """
        initializes

        Arguments:
            inputs_to_transform:
                Input features on which feature engineering/transformation is to
                be applied. By default all input features are considered.
            input_transformations:
                The transformations to be considered for input features. Default is None,
                in which case all input features are considered.

                If list, then it will be the names of transformations to be considered for
                all input features. By default following transformations are considered

                    - `minmax`  rescale from 0 to 1
                    - `center`    center the data by subtracting mean from it
                    - `scale`     scale the data by dividing it with its standard deviation
                    - `zscore`    first performs centering and then scaling
                    - `box-cox`
                    - `yeo-johnson`
                    - `quantile`
                    - `robust`
                    - `log`
                    - `log2`
                    - `log10`
                    - `sqrt`    square root

                The user can however, specify list of transformations to be considered for
                each input feature. In such a case, this argument must be a dictionary
                whose keys are names of input features and values are list of transformations.

            outputs_to_transform:
                Output features on which feature engineering/transformation is to
                be applied. If None, then transformations on outputs are not applied.
            output_transformations:
                The transformations to be considered for outputs/targets. By default
                following transformations are considered for outputs

                    - `log`
                    - `log10`
                    - `sqrt`
                    - `log2`
            models:
                The models to consider during optimzation.
            parent_iterations:
                Number of iterations for parent optimization loop
            child_iterations:
                Number of iterations for child optimization loop
            parent_algorithm:
                Algorithm for optimization of parent optimzation
            child_algorithm:
                Algorithm for optimization of child optimization
            parent_val_metric:
                Validation metric to calculate val_score in parent objective function
            child_val_metric:
                Validation metric to calculate val_score in child objective function
            parent_cross_validator:
                Whether we want to apply cross validation in parent hpo loop or not?.
            cv_child_hpo:
                Whether we want to apply cross validation in child hpo loop or not?.
                If False, then val_score will be caclulated on validation data.
                The type of cross validator used is taken from model.config['cross_validator']
            monitor:
                Nmaes of performance metrics to monitor in parent hpo loop
            mode:
                whether this is a `regression` problem or `classification`
            model_kws:
                any additional key word arguments for ai4water's Model

        """
        self.inp_to_transform = inputs_to_transform
        self.x_transformations = input_transformations
        self.y_transformations = output_transformations or DEFAULT_Y_TRANFORMATIONS

        self.mode = mode
        self.models = models
        if models is None:
            if mode == "regression":
                self.models = list(regression_space(2).keys())
            else:
                self.models = list(classification_space(2).keys())

        self.parent_iters = parent_iterations
        self.child_iters = child_iterations
        # for internal use, we keep child_iter for each estimator
        self._child_iters = {model:child_iterations for model in self.models}
        self.parent_algo = parent_algorithm
        self.child_algo = child_algorithm
        self.parent_val_metric = parent_val_metric
        self.child_val_metric = child_val_metric
        self.parent_cv = cv_parent_hpo
        self.child_cv = cv_child_hpo
        self.model_kwargs = model_kws
        self.out_to_transform = outputs_to_transform

        # self.seed = None

        if isinstance(monitor, str):
            monitor = [monitor]
        assert isinstance(monitor, list)

        self.metrics = {metric: OrderedDict() for metric in monitor}

        self.parent_suggestions = OrderedDict()

        self.parent_prefix = f"pipeline_opt_{dateandtime_now()}"

        if self.mode == "regression":
            space = regression_space(num_samples=10)
        else:
            space = classification_space(num_samples=10)

        # estimator_space contains just those models which are being considered
        self.estimator_space = {}
        for mod, mod_sp in space.items():
            if mod in self.models:
                self.estimator_space[mod] = mod_sp

    @property
    def out_to_transform(self):
        return self._out_to_transform

    @out_to_transform.setter
    def out_to_transform(self, x):
        if x:
            if isinstance(x, str):
                x = [x]
            assert isinstance(x, list)
            for i in x:
                assert i in self.output_features
        self._out_to_transform = x

    @property
    def path(self):
        return os.path.join(os.getcwd(), "results", self.parent_prefix)

    @property
    def mode(self):
        return self._mode

    @mode.setter
    def mode(self, x):
        self._mode = x

    @property
    def Metrics(self):
        if self.mode == "regression":
            return RegressionMetrics
        return ClassificationMetrics

    @property
    def input_features(self):
        if 'input_features' in self.model_kwargs:
            return self.model_kwargs['input_features']
        else:
            raise ValueError

    @property
    def output_features(self):
        if 'output_features' in self.model_kwargs:
            _output_features = self.model_kwargs['output_features']
            if isinstance(_output_features, str):
                _output_features = [_output_features]
            return _output_features
        else:
            raise ValueError

    def update_model_space(self, space:dict)->None:
        """updates or changes the space of an already existing model

        Arguments:
            space
                a dictionary whose keys are names of models and values are parameter
                space for that model.
        Returns:
            None

        Example:
            >>> pl = OptimizePipeline(...)
            >>> rf_space = {'max_depth': [5,10, 15, 20],
            >>>          'n_estimators': [5,10, 15, 20]}
            >>> pl.update_model_space({"RandomForestRegressor": rf_space})
        """
        for model, space in space.items():
            if model not in self.estimator_space:
                raise ValueError(f"{model} is not valid because it is not being considered.")
            space = to_skopt_space(space)
            self.estimator_space[model] = {'param_space': [s for s in space]}
        return

    def add_model(
            self,
            model:dict
    )->None:
        """adds a new model which will be considered during optimization.

        Example:
            >>> pl = OptimizePipeline(...)
            >>> pl.add_model({"XGBRegressor": {"n_estimators": [100, 200,300, 400, 500]}})

        Arguments:
            model:
                a dictionary of length 1 whose value should also be a dictionary
                of parameter space for that model
        """
        msg = """{} is already present. If you want to change its space, please consider" \
              using 'change_model_space' function.
              """
        for model_name, model_space in model.items():
            assert model_name not in self.estimator_space, msg.format(model_name)
            assert model_name not in self.models, msg.format(model_name)
            assert model_name not in self._child_iters, msg.format(model_name)

            model_space = to_skopt_space(model_space)
            self.estimator_space[model_name] = {'param_space': model_space}
            self.models.append(model_name)
            self._child_iters[model_name] = self.child_iters

        return

    def remove_model(self, models:Union[str, list])->None:
        """removes a model from being considered.

        Example:
            >>> pl = OptimizePipeline(...)
            >>> pl.remove_model("ExtraTreeRegressor")

        Arguments:
            models:
                name or names of model to be removed.
        """
        if isinstance(models, str):
            models = [models]

        for model in models:
            self.models.remove(model)
            self.estimator_space.pop(model)
            self._child_iters.pop(model)

        return

    def change_child_iteration(self, model:dict):
        """You may want to change the child hpo iterations for one or more models.
        For example we may want to run only 10 iterations for LinearRegression but 40
        iterations for XGBRegressor. In such a canse we can use this function to
        modify child hpo iterations for one or more models. The iterations for all
        the remaining models will remain same as defined by the user at the start.

        Example:
            >>> pl = OptimizePipeline(...)
            >>> pl.change_child_iteration({"XGBRegressor": 10})

            If we want to change iterations for more than one estimators
            >>> pl.change_child_iteration(({"XGBRegressor": 30,
            >>>                             "RandomForestRegressor": 20}))

        Arguments:
            model
                a dictionary whose keys are names of models and values are number
                of iterations for that model during child hpo
        """
        for model, _iter in model.items():
            if model not in self._child_iters:
                raise ValueError(f"{model} is not a valid model name")
            self._child_iters[model] = _iter
        return

    def space(self) -> list:
        """makes the parameter space for parent hpo"""

        append = {}
        y_categories = []

        if self.x_transformations is None:
            x_categories = DEFAULT_TRANSFORMATIONS
        elif isinstance(self.x_transformations, list):
            x_categories = self.x_transformations
        else:
            x_categories = DEFAULT_TRANSFORMATIONS
            assert isinstance(self.x_transformations, dict)

            for feature, transformation in self.x_transformations.items():
                assert isinstance(transformation, list)
                append[feature] = transformation

        if self.out_to_transform:
            # if the user has provided name of any outupt feature
            # on feature transformation is to be applied

            if isinstance(self.y_transformations, list):
                assert all([t in DEFAULT_Y_TRANFORMATIONS for t in self.y_transformations]), f"""
                transformations must be one of {DEFAULT_Y_TRANFORMATIONS}"""

                for out in self.output_features:
                    append[out] = self.y_transformations
                y_categories = self.y_transformations

            else:
                assert isinstance(self.y_transformations, dict)
                for out_feature, y_transformations in self.y_transformations.items():

                    assert out_feature in self.output_features
                    assert isinstance(y_transformations, list)
                    assert all(
                        [t in DEFAULT_Y_TRANFORMATIONS for t in self.y_transformations]), f"""
                        transformations must be one of {DEFAULT_Y_TRANFORMATIONS}"""
                    append[out_feature] = y_transformations
                y_categories = list(self.y_transformations.values())

        sp = make_space(self.inp_to_transform + (self.out_to_transform or []),
                        categories=set(x_categories + y_categories),
                        append=append)

        algos = Categorical(self.models, name="estimator")
        sp = sp + [algos]

        return sp

    @property
    def max_child_iters(self):
        return max(self._child_iters.values())

    def reset(self):

        self.parent_iter_ = 0
        self.child_iter_ = 0
        self.val_scores_ = OrderedDict()

        # each row indicates parent iteration, column indicates child iteration
        self.child_val_metrics_ = np.full((self.parent_iters, self.max_child_iters),
                                         np.nan)

        return

    def fit(
            self,
            data: pd.DataFrame,
            previous_results=None
    ) -> "ai4water.hyperopt.HyperOpt":
        """

        Arguments:
            data:
                A pandas dataframe
            previous_results:
                path of file which contains xy values.
        Returns:
            an instance of ai4water.hyperopt.HyperOpt class which is used for optimization.
        """

        self.data = data

        self.reset()

        parent_opt = HyperOpt(
            self.parent_algo,
            param_space=self.space(),
            objective_fn=self.parent_objective,
            num_iterations=self.parent_iters,
            opt_path=self.path
        )

        if previous_results is not None:
            parent_opt.add_previous_results(previous_results)

        formatter = "{:<5} {:<18} " + "{:<15} " * (len(self.metrics))
        print(formatter.format(
            "Iter",
            self.parent_val_metric,
            *[k for k in self.metrics.keys()])
        )

        res = parent_opt.fit()

        setattr(self, 'optimizer', parent_opt)

        # make a 2d array of all erros being monitored.
        errors = np.column_stack([list(v.values()) for v in self.metrics.values()])
        # add val_scores as new columns
        errors = np.column_stack([errors, list(self.val_scores_.values())])
        # save the errors being monitored
        fpath = os.path.join(self.path, "errors.csv")
        pd.DataFrame(errors,
                     columns=list(self.metrics.keys()) + ['val_scores']
                     ).to_csv(fpath)

        # save results of child iterations as csv file
        fpath = os.path.join(self.path, "child_iters.csv")
        pd.DataFrame(self.child_val_metrics_,
                     columns=[f'iter_{i}' for i in range(self.max_child_iters)]).to_csv(fpath)
        return res

    def parent_objective(
            self,
            **suggestions
    ) -> float:
        """objective function for parent hpo loop.
        This objective fuction is to optimize transformations for each input
        feature and the model.

        Arguments:
            suggestions:
                key word arguments consisting of suggested transformation for each
                input feature and the model to use
        """

        self.parent_iter_ += 1

        # self.seed = np.random.randint(0, 10000, 1).item()

        # container for transformations for all features
        x_transformations = []
        y_transformations = []

        for feature, method in suggestions.items():

            if feature in self.data:
                if method == "none":  # don't do anything with this feature
                    pass
                else:
                    # get the relevant transformation for this feature
                    t = {"method": method, "features": [feature]}

                    # some preprocessing is required for log based transformations
                    if method.startswith("log"):
                        t["treat_negatives"] = True
                        t["replace_zeros"] = True
                    elif method == "box-cox":
                        t["treat_negatives"] = True
                        t["replace_zeros"] = True
                    elif method == "sqrt":
                        t['treat_negatives'] = True

                    if feature in self.input_features:
                        x_transformations.append(t)
                    else:
                        y_transformations.append(t)

        # optimize the hyperparas of estimator using child objective
        opt_paras = self.optimize_estimator_paras(
            suggestions['estimator'],
            x_transformations=x_transformations,
            y_transformations=y_transformations or None
        )

        # fit the model with optimized hyperparameters and suggested transformations
        model = Model(
            model={suggestions["estimator"]: opt_paras},
            val_metric=self.parent_val_metric,
            verbosity=0,
            # seed=self.seed,
            x_transformation=x_transformations,
            y_transformation=y_transformations,
            prefix=self.parent_prefix,
            **self.model_kwargs
        )

        self.parent_suggestions[self.parent_iter_] = {
            # 'seed': self.seed,
            'x_transformation': x_transformations,
            'y_transformation': y_transformations,
            'model': {suggestions['estimator']: opt_paras},
            'path': model.path
        }

        if self.parent_cv:  # train the model and evaluate it to calculate val_score
            val_score = model.cross_val_score(data=self.data)
        else:  # val_score will be obtained by performing cross validation
            # train the model
            model.fit(data=self.data)
            val_score = eval_model_manually(model, self.parent_val_metric, self.Metrics)

        # calculate all additional performance metrics which are being monitored
        t, p = model.predict(data='validation', return_true=True, process_results=False)
        errors = RegressionMetrics(t, p, remove_zero=True, remove_neg=True)

        for k, v in self.metrics.items():
            v[self.parent_iter_] = getattr(errors, k)()

        self.val_scores_[self.parent_iter_] = val_score

        # print the merics being monitored
        formatter = "{:<5} {:<18.3f} " + "{:<15.7f} " * (len(self.metrics))
        print(formatter.format(
            self.parent_iter_,
            val_score,
            *[v[self.parent_iter_] for v in self.metrics.values()])
        )

        return val_score

    def optimize_estimator_paras(
            self,
            estimator: str,
            x_transformations: list,
            y_transformations: list
    ) -> dict:
        """optimizes hyperparameters of an estimator"""

        CHILD_PREFIX = f"{self.parent_iter_}_{dateandtime_now()}"

        def child_objective(**suggestions):
            """objective function for optimization of estimator parameters"""

            self.child_iter_ += 1

            # build child model
            model = Model(
                model={estimator: suggestions},
                verbosity=0,
                val_metric=self.child_val_metric,
                x_transformation=x_transformations,
                y_transformation=y_transformations,
                # seed=self.seed,
                prefix=f"{self.parent_prefix}{SEP}{CHILD_PREFIX}",
                **self.model_kwargs
            )

            if self.child_cv:
                val_score = model.cross_val_score(data=self.data)
            else:
                # fit child model
                model.fit(data=self.data)
                val_score = eval_model_manually(model, self.child_val_metric, self.Metrics)

            # populate all child val scores
            self.child_val_metrics_[self.parent_iter_-1, self.child_iter_-1] = val_score

            return val_score

        # make space
        child_space = self.estimator_space[estimator]['param_space']
        self.child_iter_ = 0  # before starting child hpo, reset iteration counter

        optimizer = HyperOpt(
            self.child_algo,
            objective_fn=child_objective,
            num_iterations=self._child_iters[estimator],
            param_space=child_space,
            verbosity=0,
            process_results=False,
            opt_path=os.path.join(self.path, CHILD_PREFIX),
        )

        optimizer.fit()

        # free memory if possible
        gc.collect()

        # return the optimized parameters
        return optimizer.best_paras()

    def get_best_metric(
            self,
            metric_name: str
    )->float:
        """returns the best value of a particular performance metric.
        The metric must be recorded i.e. must be given as `monitor` argument.
        """
        if metric_name not in self.metrics:
            raise ValueError(f"{metric_name} is not a valid metric. Available "
                             f"metrics are {self.metrics.keys()}")

        if MATRIC_TYPES[metric_name] == "min":
            return np.nanmin(list(self.metrics[metric_name].values())).item()
        else:
            return np.nanmax(list(self.metrics[metric_name].values())).item()

    def get_best_metric_iteration(
            self,
            metric_name: str
    )->int:
        """returns iteration of the best value of a particular performance metric.

        Arguments:
            metric_name:
                The metric must be recorded i.e. must be given as `monitor` argument.
        """

        if metric_name not in self.metrics:
            raise ValueError(f"{metric_name} is not a valid metric. Available "
                             f"metrics are {self.metrics.keys()}")

        if MATRIC_TYPES[metric_name] == "min":
            idx =  np.nanargmin(list(self.metrics[metric_name].values()))
        else:
            idx =  np.nanargmax(list(self.metrics[metric_name].values()))

        return int(idx+1)

    def get_best_pipeline_by_metric(
            self,
            metric_name:str
    )->dict:
        """returns the best pipeline with respect to a particular performance
        metric.

        Arguments:
            metric_name:
                The name of metric whose best value is to be retrieved. The metric
                must be recorded i.e. must be given as `monitor`.
        Returns:
            a dictionary with follwoing keys

                - `path` path where the model is saved on disk
                - `model` name of model
                - x_transfromations
                - y_transformations
        """

        idx = self.get_best_metric_iteration(metric_name)

        return self.parent_suggestions[idx]

    def get_best_pipeline_by_model(
            self,
            model_name:str,
            metric_name:str
    )->tuple:
        """returns the best pipeline with respect to a particular model and
        performance metric. The metric must be recorded i.e. must be given as
        `monitor` argument.

        Arguments:
            model_name:
                The name of model for which best pipeline is to be found. The `best`
                is defined by `metric_name`.
            metric_name:
                The name of metric with respect to which the best model is to
                be retrieved.
        Returns:
            a tuple of length two

            - first value is a float which represents the value of
            metric
            - second value is a dictionary of pipeline with four keys
                x_transformation
                y_transformation
                model
                path
        """

        if metric_name not in self.metrics:
            raise ValueError(f"{metric_name} is not a valid metric. Available "
                             f"metrics are {self.metrics.keys()}")

        model_container = {}

        for iter_num, iter_suggestions in self.parent_suggestions.items():
                model = iter_suggestions['model']
                if model_name in model:
                    print(model_name)
                    metric_val = self.metrics[metric_name][iter_num]
                    metric_val = round(metric_val, 4)

                    model_container[metric_val] = iter_suggestions

        if len(model_container)==0:
            raise ModelNotUsedError(model_name)

        container_items = model_container.items()

        sorted_container = sorted(container_items)

        return sorted_container[-1]

    def baseline_results(self, data=None)->tuple:
        """Runs all the models with their default parameters and without
        any x and y transformation. These results can be considered as
        baseline results and can be compared with optimized model's results.

        Arguments:
            data
                If given, will override data given during .fit call.

        Returns:
            a tuple of two dictionaries.
            - a dictionary of val_scores on test data for each model
            - a dictionary of metrics being monitored for  each model on test data.
        """
        val_scores = {}
        metrics = {}

        for estimator in self.models:
            # build model
            model = Model(
                model=estimator,
                verbosity=0,
                val_metric=self.parent_val_metric,
                # seed=self.seed,
                prefix=f"{self.parent_prefix}{SEP}baselines",
                **self.model_kwargs
            )

            if data is None:
                data = self.data
            model.fit(data=data)

            t,p = model.predict(return_true=True)
            errors = self.Metrics(t,p)
            val_scores[estimator] = getattr(errors, self.parent_val_metric)()

            _metrics = {}
            for m in self.metrics.keys():
                _metrics[m] = getattr(errors, m)()
            metrics[estimator] = _metrics

        return val_scores, metrics

    def dumbbell_plot(
            self,
            metric_name:str,
            figsize:tuple=None,
            show: bool = True,
            save:bool = True
    )->plt.Axes:
        """Generate Dumbbell plot as comparison of baseline models with
        optimized models.

        Arguments:
            metric_name
                The name of metric with respect to which the models have
                to be compared.
            figsize
                If given, plot will be generated of this size.
            show:

            save
                By default True. If False, function will not save the
                resultant plot in current working directory.

        Returns:
            matplotlib Axes
        """

        _, bl_results = self.baseline_results()

        bl_models = {}
        for k,v in bl_results.items():
            bl_models[k] = v[metric_name]

        optimized_models = {}

        for model_name in self.models:
            try:
                metric_val, _ = self.get_best_pipeline_by_model(model_name, metric_name)
            # the model was not used so consider the baseline result as optimzied
            # result
            except ModelNotUsedError:
                metric_val = bl_models[model_name]

            optimized_models[model_name] = metric_val

        combined = defaultdict(list)
        for d in (bl_models, optimized_models):
            for key, value in d.items():
                combined[key].append(value)

        df = pd.DataFrame.from_dict(combined).transpose()
        df = df.reset_index()
        df.columns = ['models', 'baseline', 'optimized']

        ax = dumbbell_plot(df, figsize=figsize)

        fpath = os.path.join(os.getcwd(), "results", self.parent_prefix, "dumbell")
        if save:
            plt.savefig(fpath, fpi=300)
        if show:
            plt.show()

        return ax

    def taylor_plot(
            self,
            plot_bias: bool = True,
            figsize: tuple = None,
            show: bool = True,
            save: bool = True,
            **kwargs
    ):
        """makes taylor plot using the best version of each model.
        The number of models in taylor plot will be equal to the number
        of models which have been considered by the model.

        Arguments:
            plot_bias:

            figsize:

            show:

            save:

            kwargs:
                any additional keyword arguments for taylor_plot function of ai4water.
        """
        raise NotImplementedError


def eval_model_manually(model, metric: str, Metrics) -> float:
    """evaluates the model"""
    # make prediction on validation data
    t, p = model.predict(data='validation', return_true=True, process_results=False)
    errors = Metrics(t, p, remove_zero=True, remove_neg=True)
    val_score = getattr(errors, metric)()

    metric_type = MATRIC_TYPES.get(metric, 'min')

    # the optimization will always solve minimization problem so if
    # the metric is to be maximized change the val_score accordingly
    if metric_type != "min":
        val_score = 1.0 - val_score

    # val_score can be None/nan/inf
    if not math.isfinite(val_score):
        val_score = 1.0

    return val_score

def dumbbell_plot(df:pd.DataFrame, figsize:tuple=None):
    df.sort_values('optimized', inplace=True)
    df.reset_index(inplace=True)

    # Func to draw line segment
    def newline(p1, p2, color='black'):
        ax = plt.gca()
        l = mlines.Line2D([p1[0], p2[0]], [p1[1], p2[1]], color='skyblue')
        ax.add_line(l)
        return l

    if figsize is None:
        figsize = (14,14)
    # Figure and Axes
    fig, ax = plt.subplots(1, 1, figsize=figsize, facecolor='#f7f7f7', dpi=80)

    # Vertical Lines
    ax.vlines(x=.05, ymin=0, ymax=26, color='black', alpha=1, linewidth=1, linestyles='dotted')
    ax.vlines(x=.10, ymin=0, ymax=26, color='black', alpha=1, linewidth=1, linestyles='dotted')
    ax.vlines(x=.15, ymin=0, ymax=26, color='black', alpha=1, linewidth=1, linestyles='dotted')
    ax.vlines(x=.20, ymin=0, ymax=26, color='black', alpha=1, linewidth=1, linestyles='dotted')

    # Points
    ax.scatter(y=df['index'], x=df['baseline'], s=50, color='#0e668b', alpha=0.7)
    ax.scatter(y=df['index'], x=df['optimized'], s=50, color='#a3c4dc', alpha=0.7)

    # Line Segments
    for i, p1, p2 in zip(df['index'], df['optimized'], df['baseline']):
        newline([p1, i], [p2, i])

    # Decoration
    ax.set_facecolor('#f7f7f7')
    ax.set_title("Dumbell Chart: Pct Change - 2013 vs 2014", fontdict={'size': 22})
    ax.set(xlim=(0, .25), ylim=(-1, 27), ylabel='Mean GDP Per Capita')
    ax.set_xticks([.05, .1, .15, .20])
    ax.set_xticklabels(['5%', '15%', '20%', '25%'])
    ax.set_xticklabels(['5%', '15%', '20%', '25%'])

    return ax


class ModelNotUsedError(Exception):
    def __init__(self, model_name):
        self.model = model_name

    def __str__(self):
        return f"""model {self.model} is not used during optimization"""