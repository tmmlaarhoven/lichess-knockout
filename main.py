import knockout
import sys

if __name__ == "__main__":
    ConfigFile = sys.argv[1].strip()
    LichessToken = sys.argv[2].strip()
    GitHubToken = sys.argv[3].strip()
    KO = knockout.KnockOut(LichessToken, GitHubToken, ConfigFile)
    KO.MainLoop()
