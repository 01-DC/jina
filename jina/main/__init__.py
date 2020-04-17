import sys


def _get_run_args(print_args: bool = True):
    from ..logging import default_logger
    from .parser import get_main_parser
    from ..helper import colored

    parser = get_main_parser()
    if len(sys.argv) > 1:
        args = parser.parse_args()
        default_args = parser.parse_args([sys.argv[1]])
        if print_args:
            from pkg_resources import resource_filename
            with open(resource_filename('jina', '/'.join(('resources', 'jina.logo')))) as fp:
                logo_str = fp.read()
            param_str = []
            for k, v in sorted(vars(args).items()):
                j = f'{k.replace("_", "-"): >30.30} = {str(v):30.30}'
                if getattr(default_args, k) == v:
                    param_str.append('   ' + j)
                else:
                    param_str.append('🔧️ ' + colored(j, 'blue', 'on_yellow'))
            param_str = '\n'.join(param_str)
            default_logger.info(f'\n{logo_str}\n▶️  {" ".join(sys.argv)}\n{param_str}\n')
        return args
    else:
        parser.print_help()
        exit()


def main():
    """The main entrypoint of the CLI """
    from . import api
    args = _get_run_args()
    getattr(api, args.cli.replace('-', '_'))(args)
