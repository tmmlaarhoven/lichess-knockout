import argparse
import knockout
import sys

if __name__ == "__main__":

    # Set up argument parser for the right parameters
    ArgParser = argparse.ArgumentParser(
        description = "Create a Lichess knockout tournament. \
            For more information, see https://github.com/tmmlaarhoven/lichess-knockout.",
        epilog = "All arguments are mandatory.")

    # Set up configuration file argument
    ArgParser.add_argument("-c",
        dest = "config",
        help = "File name of the configuration file",
        type = str,
        required = True)

    # Allow two options for providing the Lichess token: in a file, or directly on the command line
    LichessGroup = ArgParser.add_mutually_exclusive_group(required = True)
    LichessGroup.add_argument("-l", dest = "lichess",
        help = "Lichess token with team permissions",
        type = str)
    LichessGroup.add_argument("-lf", dest = "lichessfile",
        help = "Text file with Lichess token with team permissions",
        type = str)

    # Allow two options for providing the Lichess token: in a file, or directly on the command line
    GitHubGroup = ArgParser.add_mutually_exclusive_group(required = True)
    GitHubGroup.add_argument("-g", dest = "github",
        help = "GitHub token with repository permissions",
        type = str)
    GitHubGroup.add_argument("-gf", dest = "githubfile",
        help = "Text file with GitHub token with repository permissions",
        type = str)

    # Parse the arguments
    Args = ArgParser.parse_args()

    # Parse the parameters as appropriate
    ConfigFile = Args.config
    if Args.lichess is not None:
        LichessToken = Args.lichess
    else:
        with open(Args.lichessfile) as LichessTokenFile:
            LichessToken = LichessTokenFile.readline().strip()
    if Args.github is not None:
        GitHubToken = Args.github
    else:
        with open(Args.githubfile) as GitHubTokenFile:
            GitHubToken = GitHubTokenFile.readline().strip()

    # Set up the knockout tournament runner
    KO = knockout.KnockOut(LichessToken, GitHubToken, ConfigFile)
    KO.MainLoop()
