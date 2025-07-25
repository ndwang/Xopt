import json
import logging
from copy import deepcopy
from typing import Any, Dict, List, Optional, Union

import numpy as np
import pandas as pd
import yaml
from pandas import DataFrame
from pydantic import (
    Field,
    field_validator,
    model_validator,
    SerializeAsAny,
    ValidationInfo,
)

from xopt.evaluator import Evaluator, validate_outputs
from xopt.generator import Generator, StateOwner
from xopt.generators import get_generator
from xopt.generators.sequential import SequentialGenerator
from xopt.pydantic import XoptBaseModel
from xopt.utils import explode_all_columns
from xopt.vocs import VOCS

logger = logging.getLogger(__name__)


class Xopt(XoptBaseModel):
    """
    Object to handle a single optimization problem.

    Xopt is designed for managing a single optimization problem by unifying the
    definition, configuration, and execution of optimization tasks. It combines the
    Variables, Objective, Constraints, Statics (VOCS) definition with a generator for
    candidate generation and an evaluator for objective function evaluations.

    Parameters
    ----------
    vocs : VOCS
        VOCS object for defining the problem's variables, objectives, constraints, and
        statics.
    generator : SerializeAsAny[Generator]
        An object responsible for generating candidates for optimization.
    evaluator : SerializeAsAny[Evaluator]
        An object used for evaluating candidates generated by the generator.
    strict : bool, optional
        A flag indicating whether exceptions raised during evaluation should stop the
        optimization process.
    dump_file : str, optional
        An optional file path for dumping attributes of the xopt object and the
        results of evaluations.
    max_evaluations : int, optional
        An optional maximum number of evaluations to perform. If set, the optimization
        process will stop after reaching this limit.
    data : DataFrame, optional
        An optional DataFrame object for storing internal data related to the optimization
        process.
    serialize_torch : bool
        A flag indicating whether Torch (PyTorch) models should be serialized when
        saving them.
    serialize_inline : bool
        A flag indicating whether Torch models should be stored via binary string
        directly inside the main configuration file.

    Methods
    -------
    step()
        Executes one optimization cycle, generating candidates, submitting them for
        evaluation, waiting for evaluation results, and updating data storage.
    run()
        Runs the optimization process until the specified stopping criteria are met,
        such as reaching the maximum number of evaluations.
    evaluate(input_dict: Dict)
        Evaluates a candidate without storing data.
    evaluate_data(input_data)
        Evaluates a set of candidates, adding the results to the internal DataFrame.
    add_data(new_data)
        Adds new data to the internal DataFrame and the generator's data.
    reset_data()
        Resets the internal data by clearing the DataFrame.
    random_evaluate(n_samples=1, seed=None, **kwargs)
        Generates random inputs using the VOCS and evaluates them, adding the data to
        Xopt.
    yaml(**kwargs)
        Serializes the Xopt configuration to a YAML string.
    dump(file: str = None, **kwargs)
        Dumps the Xopt configuration to a specified file.
    dict(**kwargs) -> Dict
        Provides a custom dictionary representation of the Xopt configuration.
    json(**kwargs) -> str
        Serializes the Xopt configuration to a JSON string.
    """

    vocs: VOCS = Field(description="VOCS object for Xopt")
    generator: SerializeAsAny[Generator] = Field(
        description="generator object for Xopt"
    )
    evaluator: SerializeAsAny[Evaluator] = Field(
        description="evaluator object for Xopt"
    )
    strict: bool = Field(
        True,
        description="flag to indicate if exceptions raised during evaluation "
        "should stop Xopt",
    )
    dump_file: Optional[str] = Field(
        None, description="file to dump the results of the evaluations"
    )
    max_evaluations: Optional[int] = Field(
        None, description="maximum number of evaluations to perform"
    )
    data: Optional[DataFrame] = Field(None, description="internal DataFrame object")
    serialize_torch: bool = Field(
        False,
        description="flag to indicate that torch models should be serialized "
        "when dumping",
    )
    serialize_inline: bool = Field(
        False,
        description="flag to indicate if torch models"
        " should be stored inside main config file",
    )

    @model_validator(mode="before")
    @classmethod
    def validate_model(cls, data: Any):
        """
        Validate the Xopt model by checking the generator and evaluator.
        """
        if isinstance(data, dict):
            # validate vocs
            if isinstance(data["vocs"], dict):
                data["vocs"] = VOCS(**data["vocs"])

            # validate generator
            if isinstance(data["generator"], dict):
                name = data["generator"].pop("name")
                generator_class = get_generator(name)
                data["generator"] = generator_class.model_validate(
                    {**data["generator"], "vocs": data["vocs"]}
                )
            elif isinstance(data["generator"], str):
                generator_class = get_generator(data["generator"])

                data["generator"] = generator_class.model_validate(
                    {"vocs": data["vocs"]}
                )

            # make a copy of the generator / vocs objects to avoid modifying the original
            data["vocs"] = deepcopy(data["vocs"])
            data["generator"] = deepcopy(data["generator"])

        return data

    @field_validator("evaluator", mode="before")
    def validate_evaluator(cls, value):
        if isinstance(value, dict):
            value = Evaluator(**value)

        return value

    @field_validator("data", mode="before")
    def validate_data(cls, v, info: ValidationInfo):
        if isinstance(v, dict):
            try:
                v = pd.DataFrame(v)
                v.index = v.index.astype(np.int64)
                v = v.sort_index()
            except IndexError:
                v = pd.DataFrame(v, index=[0])
        elif isinstance(v, DataFrame):
            if not pd.api.types.is_integer_dtype(v.index):
                raise ValueError("dataframe index must be integer")
        # also add data to generator
        # TODO: find a more robust way of doing this
        generator = info.data["generator"]

        # Some generators need to maintain their own state (such as sequential generators)
        if not isinstance(generator, StateOwner):
            generator.add_data(v)
        else:
            generator.set_data(v)

        return v

    @property
    def n_data(self) -> int:
        if self.data is None:
            return 0
        else:
            return len(self.data)

    def __init__(self, *args, **kwargs):
        """
        Initialize Xopt.

        Parameters
        ----------
        args : tuple
            Positional arguments; a single YAML string can be passed as the only argument
            to initialize Xopt.
        kwargs : dict
            Keyword arguments for initializing Xopt.

        Raises
        ------
        ValueError
            If both a YAML string and keyword arguments are specified during
            initialization.
            If more than one positional argument is provided.

        Notes
        -----
        - If a single YAML string is provided in the `args` argument, it is deserialized
          into keyword arguments using `yaml.safe_load`.
        - When using the YAML string for initialization, no additional keyword arguments
          are allowed.

        """
        if len(args) == 1:
            if len(kwargs) > 0:
                raise ValueError("cannot specify yaml string and kwargs for Xopt init")
            super().__init__(**yaml.safe_load(args[0]))
        elif len(args) > 1:
            raise ValueError(
                "arguments to Xopt must be either a single yaml string "
                "or a keyword arguments passed directly to pydantic"
            )
        else:
            super().__init__(**kwargs)

    def step(self):
        """
        Run one optimization cycle.

        This method performs the following steps:
        - Determines the number of candidates to request from the generator.
        - Passes the candidate request to the generator.
        - Submits candidates to the evaluator.
        - Waits until all evaluations are finished
        - Updates data storage and generator data storage (if applicable).

        """
        logger.info("Running Xopt step")

        # get number of candidates to generate
        n_generate = self.evaluator.max_workers

        # generate samples and submit to evaluator
        logger.debug(f"Generating {n_generate} candidates")
        new_samples = self.generator.generate(n_generate)

        if new_samples is not None:
            # Evaluate data
            self.evaluate_data(new_samples)

    def run(self):
        """
        Run until the maximum number of evaluations is reached or the generator is done.

        """
        # TODO: implement stopping criteria class
        logger.info("Running Xopt")
        if self.max_evaluations is None:
            raise ValueError("max_evaluations must be set to call Xopt.run()")

        while True:
            # Stopping criteria
            if self.max_evaluations is not None:
                if self.n_data >= self.max_evaluations:
                    logger.info(
                        f"Xopt is done. Max evaluations {self.max_evaluations} reached."
                    )
                    break

            self.step()

    def evaluate(self, input_dict: Dict):
        """
        Evaluate a candidate without storing data.

        Parameters
        ----------
        input_dict : Dict
            A dictionary representing the input data for candidate evaluation.

        Returns
        -------
        Any
            The result of the evaluation.

        """
        inputs = deepcopy(input_dict)

        # add constants to input data
        for name, value in self.vocs.constants.items():
            inputs[name] = value

        self.vocs.validate_input_data(DataFrame(inputs, index=[0]))
        return self.evaluator.evaluate(input_dict)

    def evaluate_data(
        self,
        input_data: Union[
            pd.DataFrame,
            List[Dict[str, float]],
            Dict[str, List[float]],
            Dict[str, float],
        ],
    ) -> pd.DataFrame:
        """
        Evaluate data using the evaluator and wait for results.

        This method evaluates a set of candidates and adds the results to the internal
        DataFrame.

        Parameters
        ----------
        input_data : Union[pd.DataFrame, List[Dict[str, float], Dict[str, List[float],
                        Dict[str, float]]]
            The input data for evaluation, which can be provided as a DataFrame, a list of
            dictionaries, or a single dictionary.

        Returns
        -------
        pd.DataFrame
            The results of the evaluations added to the internal DataFrame.

        """
        # translate input data into pandas dataframes
        if not isinstance(input_data, DataFrame):
            try:
                input_data = DataFrame(deepcopy(input_data))
            except ValueError:
                input_data = DataFrame(deepcopy(input_data), index=[0])

        logger.debug(f"Evaluating {len(input_data)} inputs")
        self.vocs.validate_input_data(input_data)

        # add constants to input data
        for name, value in self.vocs.constants.items():
            input_data[name] = value

        # if we are using a sequential generator that is active, make sure that the evaluated data matches the last candidate
        if isinstance(self.generator, SequentialGenerator):
            if self.generator.is_active:
                self.generator.validate_point(input_data)

        output_data = self.evaluator.evaluate_data(input_data)

        if self.strict:
            validate_outputs(output_data)

        # explode any list like results if all the output names exist
        output_data = explode_all_columns(output_data)

        self.add_data(output_data)

        # dump data to file if specified
        if self.dump_file is not None:
            self.dump()

        return output_data

    def add_data(self, new_data: pd.DataFrame):
        """
        Concatenate new data to the internal DataFrame and add it to the generator's
        data.

        Parameters
        ----------
        new_data : pd.DataFrame
            New data to be added to the internal DataFrame.

        """
        logger.debug(f"Adding {len(new_data)} new data to internal dataframes")

        # Set internal dataframe.
        if self.data is not None:
            new_data = pd.DataFrame(new_data, copy=True)  # copy for reindexing
            new_data.index = np.arange(len(self.data), len(self.data) + len(new_data))
            self.data = pd.concat([self.data, new_data], axis=0)
        else:
            new_data = pd.DataFrame(new_data, copy=True)
            new_data.index = np.arange(0, len(new_data))
            self.data = new_data
        self.generator.add_data(new_data)

    def reset_data(self):
        """
        Reset the internal data by clearing the DataFrame.

        """
        self.data = pd.DataFrame()
        self.generator.data = pd.DataFrame()

    def remove_data(
        self, indices: list[int], inplace: bool = True
    ) -> Optional[pd.DataFrame]:
        """
        Removes data from the `X.data` data storage attribute.

        Parameters
        ----------
        indices: list of integers
            List of indices specifying the rows (steps) to remove from data.

        inplace: boolean, optional
            Whether to update data inplace. If False, returns a copy.

        Returns
        -------
        pd.DataFrame or None
            A copy of the internal DataFrame with the specified rows removed
            or None if inplace is True.

        """
        new_data = self.data.drop(labels=indices)
        new_data.index = np.arange(len(new_data), dtype=np.int64)
        if inplace:
            self.data = new_data
            self.generator.data = new_data
        else:
            return new_data

    def random_evaluate(
        self,
        n_samples=None,
        seed=None,
        custom_bounds: dict = None,
    ):
        """
        Convenience method to generate random inputs using VOCs and evaluate them.

        This method generates random inputs using the Variables, Objectives,
        Constraints, and Statics (VOCS) and evaluates them, adding the data to the
        Xopt object and generator.

        Parameters
        ----------
        n_samples : int, optional
            The number of random samples to generate.
        seed : int, optional
            The random seed for reproducibility.
        custom_bounds : dict, optional
            Dictionary of vocs-like ranges for random sampling


        Returns
        -------
        pd.DataFrame
            The results of the evaluations added to the internal DataFrame.

        """
        random_inputs = self.vocs.random_inputs(
            n_samples, seed=seed, custom_bounds=custom_bounds, include_constants=True
        )
        result = self.evaluate_data(random_inputs)
        return result

    def grid_evaluate(
        self,
        n_samples: Union[int, Dict[str, int]],
        custom_bounds: dict = None,
    ):
        """
        Evaluate a meshgrid of points using the VOCS and add the results to the internal
        DataFrame.

        Parameters
        ----------
        n_samples : int or dict
            The number of samples along each axis to evaluate on a meshgrid.
            If an int is provided, the same number of samples is used for all axes.
        custom_bounds : dict, optional
            Dictionary of vocs-like ranges for mesh sampling.

        Returns
        -------
        pd.DataFrame
            The results of the evaluations added to the internal DataFrame.
        """
        grid_inputs = self.vocs.grid_inputs(n_samples, custom_bounds=custom_bounds)
        result = self.evaluate_data(grid_inputs)
        return result

    def yaml(self, **kwargs):
        """
        Serialize the Xopt configuration to a YAML string.

        Parameters
        ----------
        **kwargs
            Additional keyword arguments for customizing serialization.

        Returns
        -------
        str
            The Xopt configuration serialized as a YAML string.

        """
        output = json.loads(
            self.json(
                serialize_torch=self.serialize_torch,
                serialize_inline=self.serialize_inline,
                **kwargs,
            )
        )
        return yaml.dump(output)

    def dump(self, file: str = None, **kwargs):
        """
        Dump data to a file.

        Parameters
        ----------
        file : str, optional
            The path to the file where the Xopt configuration will be dumped.
        **kwargs
            Additional keyword arguments for customizing the dump.

        Raises
        ------
        ValueError
            If no dump file is specified via argument or in the `dump_file` attribute.

        """
        fname = file if file is not None else self.dump_file

        if fname is None:
            raise ValueError(
                "no dump file specified via argument or in `dump_file` attribute"
            )
        else:
            with open(fname, "w") as f:
                f.write(self.yaml(**kwargs))
            logger.debug(f"Dumped state to YAML file: {fname}")

    def dict(self, **kwargs) -> Dict:
        """
        Handle custom dictionary generation.

        Parameters
        ----------
        **kwargs
            Additional keyword arguments for customizing the dictionary generation.

        Returns
        -------
        Dict
            A dictionary representation of the Xopt configuration.

        """
        result = super().model_dump(**kwargs)
        result["generator"] = {"name": self.generator.name} | result["generator"]
        return result

    def json(self, **kwargs) -> str:
        """
        Handle custom serialization of generators and DataFrames.

        Parameters
        ----------
        **kwargs
            Additional keyword arguments for customizing serialization.

        Returns
        -------
        str
            The Xopt configuration serialized as a JSON string.

        """
        result = super().to_json(**kwargs)
        dict_result = json.loads(result)
        dict_result["generator"] = {"name": self.generator.name} | dict_result[
            "generator"
        ]
        dict_result["data"] = (
            json.loads(self.data.to_json()) if self.data is not None else None
        )

        # TODO: implement version checking
        # dict_result["xopt_version"] = __version__

        return json.dumps(dict_result)

    def __repr__(self):
        """
        Return information about the Xopt object, including the YAML representation
        without data.

        Returns
        -------
        str
            A string representation of the Xopt object.

        """
        # lazy import to avoid circular import
        from xopt import __version__

        # get dict minus data
        config = json.loads(self.json())
        config.pop("data")
        return f"""
            Xopt
________________________________
Version: {__version__}
Data size: {self.n_data}
Config as YAML:
{yaml.dump(config)}
"""

    def __str__(self):
        """
        Return a string representation of the Xopt object.

        Returns
        -------
        str
            A string representation of the Xopt object.

        """
        return self.__repr__()
