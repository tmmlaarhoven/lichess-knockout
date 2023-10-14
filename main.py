import argparse
import knockout
import sys

if __name__ == "__main__":

    # Set up argument parser for the right parameters
    ArgParser = argparse.ArgumentParser(
        prog = "Lichess Knockout Tournament Tool",
        description = "Create a Lichess knockout tournament. \
            For more information, see https://github.com/tmmlaarhoven/lichess-knockout.",
        epilog = "All arguments are mandatory.")
    ArgParser.add_argument("-c", "--config",
        help = "File name of the configuration file",
        type = str,
        required = True)
    ArgParser.add_argument("-l", "--lichess",
        help = "Lichess token with team permissions",
        type = str,
        required = True)
    ArgParser.add_argument("-g", "--github",
        help = "GitHub token with repository permissions",
        type = str,
        required = True)
    Args = ArgParser.parse_args()

    # Parse the parameters as appropriate
    ConfigFile = Args.config
    LichessToken = Args.lichess
    GitHubToken = Args.github

    # Set up the knockout tournament runner
    KO = knockout.KnockOut(LichessToken, GitHubToken, ConfigFile)
    KO.MainLoop()
