import argparse
import logging
import re

from aim import Run
from hydra import compose, initialize
from hydra.core.plugins import Plugins
from omegaconf import OmegaConf
from urartu.commands.command import Command
from urartu.utils.launcher import launch, launch_on_slurm
from urartu.utils.slurm import is_submitit_available
from urartu.utils.registry import Registry
from urartu.utils.hydra_plugin import UrartuPlugin

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def register_my_plugin() -> None:
    Plugins.instance().register(UrartuPlugin)


register_my_plugin()


@Command.register("launch")
class Launch(Command):
    """
    Launches an action from a specific module
    """

    def add_subparser(self, parser: argparse._SubParsersAction) -> argparse.ArgumentParser:
        description = """urartu: launcher"""
        available_modules = ", ".join(Registry.load_file_content().keys())
        subparser = parser.add_parser(
            self.name,
            description=description,
            help=f"Launch an action from a given module: {available_modules}",
        )

        subparser.add_argument("--name", type=str, help="name of the project/module")
        subparser.add_argument("module_args", nargs=argparse.REMAINDER, help="module arguments")

        subparser.set_defaults(fire=self._launch)

        return subparser

    def _launch(self, args: argparse.Namespace):
        module_name = re.sub(r"[^A-Za-z0-9]+", "", args.name)
        module_path = Registry.get_module_path_by_name(module_name)

        with initialize(version_base=None, config_path="../config"):
            cfg = compose(config_name="main", overrides=args.module_args)
        cfg = OmegaConf.create(OmegaConf.to_container(cfg, resolve=True, enum_to_str=True))

        aim_run = Run(
            repo=cfg.aim.repo,
            experiment=cfg.action_config.experiment_name,
            log_system_params=cfg.aim.log_system_params,
        )
        aim_run.set("cfg", cfg, strict=False)

        if cfg.slurm.use_slurm:
            assert is_submitit_available(), "Please 'pip install submitit' to schedule jobs on SLURM"

            launch_on_slurm(
                module=module_path,
                action_name=cfg.action_name,
                cfg=cfg,
                aim_run=aim_run,
            )
        else:
            launch(
                module=module_path,
                action_name=cfg.action_name,
                cfg=cfg,
                aim_run=aim_run,
            )

        if aim_run.active:
            aim_run.close()
