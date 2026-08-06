import logging
import sys

from murineshiftwork.cli.evaluate import evaluate_args
from murineshiftwork.cli.parser import parse_args
from murineshiftwork.hardware.bpod import BpodConnectionError, patch_user_settings
from murineshiftwork.logic.log import patch_logging_levels


def _print_run_banner():
    from murineshiftwork.logic.versions import msw_version_banner

    banner = msw_version_banner()
    print(banner)
    # log it too, so the exact software provenance lands in the session log
    logging.getLogger("murineshiftwork").info("msw packages:\n%s", banner)


def run_cli(*args):
    """Command line interface for Murine Shift Work."""
    patch_logging_levels()
    patch_user_settings()

    if not args:
        args = sys.argv[1:]

    if len(args) > 0 and not isinstance(args[0], str):
        args = args[0]

    if len(args) > 0 and str(args[0]).endswith(".py"):
        _, args = args[0], args[1:]

    if len(args) <= 1:
        args = args + ["-h"]

    args_dict = parse_args(args=args)

    # Only `run` needs the hardware/subject/task context built by evaluate_args.
    # All other commands - built-ins (init, setup, ...) and plugin subcommands
    # registered via the msw.cli entry-point group (e.g. `oe`) - dispatch
    # directly, so they must not be forced through run-task arg evaluation.
    if args_dict.get("command") == "run":
        _print_run_banner()
        args_dict = evaluate_args(args_dict=args_dict)
        if "exit_flag" in args_dict:
            return

    # Call module. A Bpod connection failure raises BpodConnectionError (instead of the library
    # calling sys.exit); the operator already saw the boxed message, so translate it to exit 1
    # here at the CLI boundary. A GUI/RPC caller catches the exception instead.
    try:
        args_dict["func"](**args_dict)
    except BpodConnectionError:
        sys.exit(1)

    logging.debug("EXITING CLI.")


if __name__ == "__main__":
    run_cli()
