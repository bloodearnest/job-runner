from argparse import ArgumentParser

from jobrunner.main import watch

if __name__ == "__main__":
    parser = ArgumentParser(
        description="Run in a loop, watching for jobs, and executing them"
    )
    subparsers = parser.add_subparsers(help="sub-command help", dest="subparser_name")
    parser_watch = subparsers.add_parser(
        "watch", help="Poll a remote server for cohort builds"
    )
    parser_watch.add_argument(
        "queue_endpoint", help="URL of job queue endpoint", type=str,
    )
    options = parser.parse_args()
    if options.subparser_name == "watch":
        watch(options.queue_endpoint)
    else:
        parser.print_help()
