"""
Copyright (c) Thijs Laarhoven, 2023
Contact: mail@thijs.com

This is an automated script to:
- Schedule knock-out tournaments;
- Listen to participants joining/leaving;
- Start the tournament if the maximum number of
participants has been reached;
- Generate and ensure balanced seeded pairings;
- "Eliminating" eliminated players from the event;
- Making and updating visual tournament brackets.

This script is further integrated with GitHub Actions,
so that the script can be run from the cloud to
automatically host tournaments at given intervals,
and so that the tournament bracket can automatically
be generated and stored in a GitHub repository.

Among others, this script makes use of:
- The Lichess API
    - https://lichess.org/api
- GitHub Actions
    - https://docs.github.com/en/actions

Note: This script no longer makes use of berserk,
the Python interface for the Lichess API:
- https://pypi.org/project/berserk/
- https://github.com/lichess-org/berserk
This interface is unfortunately far from complete,
and too much functionality is missing that switching
between berserk and manual requests is not worth it.
If the missing functionality related to swiss events
gets added, the script can be adapted to only use
berserk and not needing manual API requests.
"""
import configparser
import datetime
import github
import json
import math
import matplotlib as mpl
import matplotlib.pyplot as plt
import os
import random
import requests
import sys
import time
import trees

# Class to keep track of variables
class KnockOut:
    """
    KnockOut class, for organizing knock-out events through Lichess Swiss events.

    Handles Lichess API queries internally, providing everything needed to run
    a KO tournament without further human intervention.
    """

    def __init__(self, LichessToken, GitHubToken, ConfigFile):
        """
        Initialize a new KO tournament object.
        """
        # Load configuration variables
        self._ConfigFile        = ConfigFile
        Config = configparser.ConfigParser()
        Config.read(self._ConfigFile)
        self._GitHubUserName    = Config["GitHub"]["Username"].strip()
        self._GitHubRepoName    = Config["GitHub"]["Repository"].strip()
        self._TeamId            = Config["Lichess"]["TeamId"].strip()
        self._MaxParticipants   = int(Config["Options"]["MaxParticipants"])
        self._MinParticipants   = int(Config["Options"]["MinParticipants"])
        self._GamesPerMatch     = int(Config["Options"]["GamesPerMatch"])
        self._Rated             = Config["Options"].getboolean("Rated")
        self._Variant           = Config["Options"]["Variant"].strip()
        self._ClockInit         = int(Config["Options"]["ClockInit"])
        self._ClockInc          = int(Config["Options"]["ClockInc"])
        self._ChatFor           = int(Config["Options"]["ChatFor"])       # 0: None; 10: Team leaders; 20: Team members; 30 Lichess
        self._RandomizeSeeds    = Config["Options"].getboolean("RandomizeSeeds")
        self._Title             = Config["Options"]["EventName"].strip()
        self._MinutesToStart    = int(Config["Options"]["MinutesToStart"])
        self._TieBreak          = Config["Options"]["TieBreak"]

        # Check validity of input data
        AllowedTitleChars = "abcdefghijklmnopqrstuvwxyz0123456789 ,.-"
        assert (self._ClockInit in {0, 15, 30, 45, 60, 90, 120, 180, 240, 300,
                                    360, 420, 480, 600, 900, 1200, 1500, 1800,
                                    2400, 3000, 3600, 4200, 4800, 5400, 6000,
                                    6600, 7200, 7800, 8400, 9000, 9600, 10200,
                                    10800}), "Invalid clock time!"
        assert (self._ClockInc in range(121)), "Invalid clock increment!"
        assert (self._ChatFor in {0, 10, 20, 30}), "Invalid ChatFor parameter!"
        assert (all([x.lower() in AllowedTitleChars for x in self._Title])), "Illegal characters in title!"
        assert (self._MinutesToStart >= 5), "Cannot start in less than 5 minutes!"
        assert (self._TieBreak in {"rating", "color"}), "Improper tiebreak specified!"
        assert ((self._TieBreak != "color") or (self._GamesPerMatch % 2 == 1)), "Deciding winner by color only possible for odd match lengths!"

        # Variables which should not be modified
        self._LichessToken = LichessToken
        self._GitHubToken = GitHubToken
        self._SwissId = None
        self._SwissUrl = None
        self._Winner = None
        self._Loser = None
        self._MatchRounds = math.ceil(math.log2(self._MaxParticipants))
        self._TotalRounds = self._GamesPerMatch * self._MatchRounds
        self._TreeSize = -1                         # If e.g. 5 players, it is 8

        # Decide white/black in initial games in each match
        # - Value 0 gives bottom player in bracket white first
        # - Value 1 gives top player in bracket white first
        self._TopGetsWhite = random.randrange(0, 2)

        self._Description = f"Event starts at {self._MaxParticipants} players. "
        self._Description = self._Description + f"Each match consists of {self._GamesPerMatch} game(s). "
        if self._TieBreak == "color":
            self._Description = self._Description + "In case of a tie, the player with more black games in the match advances. "
        else:
            self._Description = self._Description + "In case of a tie, the lower-rated player (at the start of the event) advances. "
        self._Started = False
        self._UnconfirmedParticipants = dict()      # The players registered on Lichess
        self._Participants = dict()                 # The players reg. on Lichess, confirmed to play, with scores
        self._AllowedPlayers = ""
        self._CurGame = -1
        self._CurMatch = -1

        assert (self._TotalRounds >= 3), "Parameters indicate not enough rounds"

        # List of lists of implicit pairings
        # Pairings = [[(A, True, [1, 1, 0, 1]), (B, False, [0, 0, 1, 0]), C, D, E, F, G, H], [A, D*, F*, H], [D*, F]]
        # Means:      [A-B,  C-D,  E-F,  G-H]   [A-D,  F-H]   [D-F]
        # Based on tree pairings from trees.py
        # Add * to show a player advances
        self._Pairings = []
        self._CurPairings = None        # API string to pass to Lichess

        # Start at specified in configuration, rounded to multiple of 10 minutes
        self._StartTime = 1000 * round(time.time()) + 60 * 1000 * self._MinutesToStart
        self._StartTime = 600000 * (self._StartTime // 600000)     # Round to multiple of 10 minutes

        # Bracket drawing: foreground colors
        self._Bracket_ColorName    = (186/256, 186/256, 186/256)
        self._Bracket_ColorWin     = ( 98/256, 153/256,  36/256)
        self._Bracket_ColorLoss    = (204/256,  51/256,  51/256)
        self._Bracket_ColorDraw    = (123/256, 153/256, 153/256)
        self._Bracket_ColorArrow   = ( 60/256,  60/256,  60/256)

        # Background colors
        self._Bracket_ColorBGAll   = ( 22/256,  21/256,  18/256)
        self._Bracket_ColorBGScore = ( 38/256,  36/256,  33/256)
        self._Bracket_ColorBGName  = ( 58/256,  56/256,  51/256)

        # Create a bracket image folder, if it does not exist
        if not os.path.exists("png"):
            os.makedirs("png")
        if not os.path.exists("logs"):
            os.makedirs("logs")

        # No file name known yet, so no log yet
        self._LogFile = None

        # Set up a github authentication workflow
        auth = github.Auth.Token(self._GitHubToken)
        g = github.Github(auth = auth)
        self._GitHubRepo = g.get_user().get_repo(self._GitHubRepoName)



    # =======================================================
    #       EXTERNAL FUNCTIONS
    # =======================================================

    def tprint(self, s):
        ToPrint = f"{datetime.datetime.now().strftime('%H:%M:%S')}: {s}"
        print(ToPrint)
        if self._LogFile is not None:
            self._LogFile.write(ToPrint + "\n")



    def PrintParticipants(self):
        """
        Print the list of participants to the standard output.
        Assumption: The list of participants is up to date.
        """
        self.tprint("")
        self.tprint("=== LIST OF PARTICIPANTS ===")
        if len(self._Participants) == 0:
            self.tprint("  (No participants yet.)")
        else:
            for Seed, (ParticipantName, Participant) in enumerate(self._Participants.items()):
                self.tprint(f"{(Seed + 1):>2}. {Participant['username']:<20} ({Participant['rating']:>4}) [{ParticipantName}]")
        self.tprint("")



    def PrintMatches(self):
        """
        Print the list of matches for this round.
        Assumption: The list of matches is up to date.
        """
        self.tprint("")
        self.tprint("=== LIST OF MATCHES ===")
        if len(self._Pairings) == 0:
            self.tprint("  (No matches yet.)")
        else:
             for i in range(len(self._Pairings[-1]) // 2):
                MatchScores = ",".join([f"{x}-{1-x}" for x in self._Pairings[-1][2*i][2]])
                self.tprint(f"{self._Pairings[-1][2*i][0]:>20} - {self._Pairings[-1][2*i+1][0]:<20} : {MatchScores}")
        self.tprint("")



    def GetRound(self) -> int:
        return self._CurMatch * self._GamesPerMatch + self._CurGame



    # =======================================================
    #       BRACKET VISUALIZATION FUNCTIONS
    # =======================================================

    def _Bracket_GetCoordinates(self, r, i):
        """
        Get the coordinates of the i'th match in round r.
        """
        x = r * self._Xw
        y = (2**r - 1) * (self._Yh + self._Ys) / 2 + 2**r * i * (self._Yh + self._Ys)
        return (x, y)



    def _Bracket_FormatScore(self, s):
        """
        Format a score without decimals and with halves.
        """
        if s < 0.8:
            return "½" if (round(2*s) % 2 == 1) else "0"
        else:
            return str(round(2*s) // 2) + ("½" if (round(2*s) % 2 == 1) else "")



    def _Bracket_Initialize(self):
        """
        Initialize drawing, and instantiate variables.
        """
        # Bracket parameters
        self._WinScore = self._GamesPerMatch / 2. + 0.2     # In case floats/rounding gives issues

        # Bracket width parameters
        self._Xn = 8      # Width of the name within the block
        self._Xg = 1      # Width of a single game result in a block
        self._Xs = 2      # Space left of block where line changes direction
        self._Xw = self._Xn + self._GamesPerMatch * self._Xg + 4     # Total width of a gamematch block WITH PADDING
        self._Xtotal = (self._MatchRounds - 1) * self._Xw + (self._Xn + self._GamesPerMatch * self._Xg)

        # Bracket height parameters
        self._Yh = 2      # Height of match block WITHOUT PADDING
        self._Ys = 1      # Vertical spacing between blocks
        self._Ytotal = self._TreeSize // 2 * (self._Yh + self._Ys) - self._Ys
        self._Ytotal += 2       # Make room for round titles

        # Initialize new, empty figure
        plt.close()
        plt.figure()
        plt.style.use(['dark_background'])
        self._fig, self._ax = plt.subplots(figsize=(self._Xtotal/2,self._Ytotal/2))
        self._fig.patch.set_facecolor(self._Bracket_ColorBGAll)

        # Temporary, remove later
        self._userchars = "abcdefghijklmnopqrstuvwxyz0123456789-_"

        # List of display information depending on win/loss/draw
        self._Bracket_DisplayScores = []
        self._Bracket_DisplayScores.append({"Color": self._Bracket_ColorLoss, "Weight": "normal"})
        self._Bracket_DisplayScores.append({"Color": self._Bracket_ColorDraw, "Weight": "normal"})
        self._Bracket_DisplayScores.append({"Color": self._Bracket_ColorWin,  "Weight": "bold"})



    def _Bracket_DrawMatchBlock(self, r, i):
        """
        Draw the blocks representing matches.
        """
        # Name box
        (XBase, YBase) = self._Bracket_GetCoordinates(r, i)
        self._ax.add_patch(mpl.patches.Rectangle((XBase, YBase), self._Xn, self._Yh,
                     facecolor = self._Bracket_ColorBGName,
                     fill = True,
                     lw = 0))
        # Score box
        self._ax.add_patch(mpl.patches.Rectangle((XBase + self._Xn, YBase), self._GamesPerMatch * self._Xg + 0.2, self._Yh,
                     facecolor = self._Bracket_ColorBGScore,
                     fill = True,
                     lw = 0))



    def _Bracket_DrawArrow(self, r, i):
        """
        Draw arrow to next round block.
        """
        # Fetch base coordinates of the block with this match
        (XBase, YBase) = self._Bracket_GetCoordinates(r, i)

        # First go right
        plt.arrow(XBase, YBase + self._Yh / 2,
                  self._Xw - self._Xs, 0,
                  width = 0.05,
                  head_width = 0,
                  head_length = 0,
                  color = self._Bracket_ColorArrow)

        # Next go up/down depending on parity
        sgn = (1 if (i % 2 == 0) else -1)
        plt.arrow(XBase + (self._Xw - self._Xs),
                  YBase + self._Yh / 2,
                  0,
                  sgn * 2**(r-1) * (self._Yh + self._Ys),
                  width = 0.05,
                  head_width = 0,
                  head_length = 0,
                  color = self._Bracket_ColorArrow)

        # Then go right
        plt.arrow(XBase + (self._Xw - self._Xs),
                  YBase + self._Yh / 2 + sgn * 2**(r-1) * (self._Yh + self._Ys),
                  self._Xs,
                  0,
                  width = 0.05,
                  head_width = 0,
                  head_length = 0,
                  color = self._Bracket_ColorArrow)



    def _Bracket_DrawRoundTitles(self):
        """
        Draw titles above the rounds, to indicate which
        rounds these are.
        """
        # Initialize naming
        RoundNames = {1: "Finals",
                      2: "Semifinals",
                      4: "Quarterfinals"}
        for i in range(3, 15):
            RoundNames[2**i] = f"Round of {2**(i+1)}"

        # Draw titles for each round
        for r in range(round(math.log2(self._TreeSize))):
            MatchesLeft = self._TreeSize // (2 ** (r + 1))
            plt.text((self._Xn + self._GamesPerMatch * self._Xg)/2 + r * self._Xw,
                     self._Ytotal - 1,
                     RoundNames[MatchesLeft],
                     fontsize = 20,
                     fontweight = "bold",
                     ha = "center",
                     va = "center",
                     color = self._Bracket_ColorName)



    def _Bracket_FillMatchBlock(self, r, i):
        """
        Fill the match block at coordinate x, y with data.
        """
        # Fill block starting from the top for match i
        # Compute ip as the ith block from the top
        ip = self._TreeSize // (2 ** (r + 1)) - i - 1
        (XBase, YBase) = self._Bracket_GetCoordinates(r, ip)

        # Get user information
        UserName1 = self._Pairings[r][2*i][0]
        User1 = self._Participants.get(UserName1,
                    {"username": "BYE", "rating": 0, "points": 0.0, "seed": -1})
        User1Won = (2 if self._Pairings[r][2*i][1] else
                    0 if self._Pairings[r][2*i+1][1] else 1)
        UserScores1 = self._Pairings[r][2*i][2]
        UserScoreStr1 = self._Bracket_FormatScore(sum(UserScores1))

        UserName2 = self._Pairings[r][2*i+1][0]
        User2 = self._Participants.get(UserName2,
                    {"username": "BYE", "rating": 0, "points": 0.0, "seed": -1})
        User2Won = (2 if self._Pairings[r][2*i+1][1] else
                    0 if self._Pairings[r][2*i][1] else 1)
        UserScores2 = self._Pairings[r][2*i+1][2]
        UserScoreStr2 = self._Bracket_FormatScore(sum(UserScores2))

        MatchUsers = [User1, User2]
        MatchUserWon = [User1Won, User2Won]
        MatchUserScores = [UserScores1, UserScores2]
        MatchUserScoreStr = [UserScoreStr1, UserScoreStr2]

        # Fill in each of the two users in the match, from top to bottom
        for j in range(2):

            # Put name and rating
            UserString = f"{MatchUsers[j]['seed']}. {MatchUsers[j]['username']} ({MatchUsers[j]['rating']})"
            if MatchUsers[j]['username'].lower() == "bye":
                UserString = "BYE"
            plt.text(XBase + 0.3,
                     YBase + self._Yh - (self._Yh / 4) - j * self._Yh / 2,
                     UserString,
                     fontsize = 13,
                     fontweight = self._Bracket_DisplayScores[MatchUserWon[j]]["Weight"],
                     ha = "left",
                     va = "center",
                     color = self._Bracket_ColorName)

            # Put total score
            plt.text(XBase + self._Xn - 0.8,
                     YBase + self._Yh - self._Yh / 4 - j * self._Yh / 2,
                     MatchUserScoreStr[j],
                     fontsize = 16,
                     fontweight = self._Bracket_DisplayScores[MatchUserWon[j]]["Weight"],
                     ha = "center",
                     va = "center",
                     color = self._Bracket_DisplayScores[MatchUserWon[j]]["Color"])

            # Put game results
            for g in range(len(MatchUserScores[j])):
                plt.text(XBase + self._Xn + 0.1 + (self._Xg / 2) + g * self._Xg,
                         YBase + self._Yh - self._Yh / 4 - j * self._Yh / 2,
                         self._Bracket_FormatScore(MatchUserScores[j][g]),
                         fontsize = 16,
                         fontweight = self._Bracket_DisplayScores[round(2*MatchUserScores[j][g])]["Weight"],
                         ha = "center",
                         va = "center",
                         color = self._Bracket_DisplayScores[round(2*MatchUserScores[j][g])]["Color"])



    def _Bracket_DrawWinners(self):
        """
        Draw trophies and add names of winners/losers.
        Only if the tournament finished, and only if
        the tournament bracket is bigger than 4 players.
        """
        assert (self._TreeSize > 4), "Cannot draw trophies with only 4 players."
        assert (self._CurMatch == self._MatchRounds - 1), "Not in the last match yet."
        assert (self._Winner is not None), "No winner yet."
        assert (self._Loser is not None), "No loser yet."

        # Show winner with trophy
        im = f"trophies/lichess-gold.png"
        img = plt.imread(im)
        imgar = 1.0
        Xmin = (self._Xn + self._GamesPerMatch * self._Xg)/2 + (self._MatchRounds - 1) * self._Xw
        Xmin = Xmin - 1.5
        Xmax = Xmin + 3
        Ymin = self._Ytotal / 2 + 1.3
        Ymax = Ymin + (Xmax - Xmin) / imgar
        plt.imshow(img, extent = (Xmin, Xmax, Ymin, Ymax))
        plt.text(Xmin + 1.5,
                Ymin - 0.5,
                self._Participants[self._Winner]["username"],
                    fontsize = 18,
                    fontweight = "bold",
                    ha = "center",
                    va = "center",
                    color = (255/255, 203/255, 55/255))

        # Show finals loser with trophy
        im2 = f"trophies/lichess-silver.png"
        img2 = plt.imread(im2)
        imgar2 = 1.0
        Xmin = (self._Xn + self._GamesPerMatch * self._Xg)/2 + (self._MatchRounds - 1) * self._Xw
        Xmin = Xmin - 1
        Xmax = Xmin + 2
        Ymin = self._Ytotal / 2 - 5
        Ymax = Ymin + (Xmax - Xmin) / imgar2
        plt.imshow(img2, extent = (Xmin, Xmax, Ymin, Ymax))
        plt.text(Xmin + 1,
                Ymin - 0.5,
                self._Participants[self._Loser]["username"],
                    fontsize = 16,
                    fontweight = "bold",
                    ha = "center",
                    va = "center",
                    color = (207/255, 194/255, 170/255))



    def _Bracket_DrawEmptyScheme(self):
        """
        Based on tree size, draw an empty bracket in proper style.
        """
        assert (self._TreeSize <= 512), "Too big to draw!"
        assert (self._TreeSize >= 4), "Too small for a tournament!"
        assert (self._TreeSize in {4, 8, 16, 32, 64, 128, 256, 512}), "Tree size not a power of two!"
        for r in range(round(math.log2(self._TreeSize))):
            self._Bracket_DrawRoundTitles()
            for i in range(self._TreeSize // (2 ** (r + 1))):
                self._Bracket_DrawMatchBlock(r, i)
                if r < round(math.log2(self._TreeSize)) - 1:
                    self._Bracket_DrawArrow(r, i)



    def _Bracket_FillScheme(self):
        """
        Based on pairing data, fill scheme with data and results.
        """
        for r in range(len(self._Pairings)):
            for i in range(self._TreeSize // (2 ** (r + 1))):
                self._Bracket_FillMatchBlock(r, i)



    def _Bracket_Save(self):
        """
        Once the bracket is complete, save it to a file.
        """
        plt.axis("off")
        plt.xlim(0, self._Xtotal)
        plt.ylim(0, self._Ytotal)
        self._fig.tight_layout()
        plt.savefig(f"png{os.sep}{self._SwissId}.png")



    def _Bracket_Upload(self, New = False):
        """
        Once the bracket image has been generated, upload it.
        """
        # Load contents to upload
        with open(f"png{os.sep}{self._SwissId}.png", "rb") as file:
            content = file.read()
            image_data = bytearray(content)
            image_bytes = bytes(image_data)

        # Upload to github
        git_file = f"png/{self._SwissId}.png"
        if New:
            self._GitHubRepo.create_file(git_file,
                                         f"Creating new bracket {self._SwissId}.png",
                                         image_bytes,
                                         branch="main")
            self.tprint("Uploaded new bracket!")
        else:
            contents = self._GitHubRepo.get_contents(git_file)
            self._GitHubRepo.update_file(contents.path,
                                         f"Updating bracket {self._SwissId}.png",
                                         image_bytes,
                                         contents.sha,
                                         branch="main")
            self.tprint("Uploaded updated bracket!")



    def _Bracket_MakeBracket(self, New = False):
        """
        Main routine for drawing a bracket.
        """
        self._Bracket_Initialize()
        self._Bracket_DrawEmptyScheme()
        self._Bracket_FillScheme()
        if self._Winner is not None and self._TreeSize > 5:
            self._Bracket_DrawWinners()
        self._Bracket_Save()
        self._Bracket_Upload(New)



    # =======================================================
    #       LICHESS FUNCTIONS
    # =======================================================

    def _LichessCreate(self):
        """
        Set up a new Lichess Swiss tournament via the API.
        """

        self.tprint("Creating new Lichess Swiss tournament...")

        # Create Lichess Swiss tournament
        Response = requests.post(f"https://lichess.org/api/swiss/new/{self._TeamId}",
                                headers = {"Authorization": f"Bearer {self._LichessToken}"},
                                data = {"name": self._Title,
                                        "clock.limit": self._ClockInit,
                                        "clock.increment": self._ClockInc,
                                        "nbRounds": self._TotalRounds,
                                        "startsAt": self._StartTime,
                                        "roundInterval": 99999999,
                                        "variant": self._Variant,
                                        "description": self._Description,
                                        "rated": ("true" if self._Rated else "false"),
                                        "chatFor": self._ChatFor})
        time.sleep(3)
        print(Response)
        print(Response.json())

        # Parse response as JSON
        JResponse = Response.json()

        # Store some data in the object
        self._SwissId = JResponse["id"]
        self._SwissUrl = f"https://lichess.org/swiss/{self._SwissId}"
        self._LogFile = open(f"logs{os.sep}{self._SwissId}.txt", "w")

        self.tprint("Opened a new log file.")

        # Update the tournament description
        self._Description = self._Description + f"\n\nBracket: https://raw.githubusercontent.com/{self._GitHubUserName}/{self._GitHubRepoName}/main/png/{self._SwissId}.png"
        #self.tprint(self._Description)
        Response = requests.post(f"https://lichess.org/api/swiss/{self._SwissId}/edit",
                                headers = {"Authorization": f"Bearer {self._LichessToken}"},
                                data = {"clock.limit": self._ClockInit,
                                        "clock.increment": self._ClockInc,
                                        "nbRounds": self._TotalRounds,
                                        "description": self._Description})
        time.sleep(3)

        self.tprint(f"Finished creating a new Lichess swiss tournament!")
        self.tprint(f"Tournament available at {self._SwissUrl}.")



    def _LichessStart(self):
        """
        Start the tournament on Lichess, and wrap up some final actions.
        """
        self.tprint("Starting Lichess tournament...")

        # Set list of allowed participants in API to current list of participants
        self._AllowedPlayers = "\n".join(self._Participants.keys())
        Response = requests.post(f"https://lichess.org/api/swiss/{self._SwissId}/edit",
                                headers = {"Authorization": f"Bearer {self._LichessToken}"},
                                data = {"clock.limit": self._ClockInit,
                                        "clock.increment": self._ClockInc,
                                        "nbRounds": self._TotalRounds,
                                        "conditions.allowList": self._AllowedPlayers})
        time.sleep(3)

        # Message players who were left out
        for UserName in self._UnconfirmedParticipants:
            if UserName not in self._Participants:
                self.tprint(f"Sorry {UserName}, you were too late!")

        self.tprint("Finished starting Lichess tournament!")



    # =======================================================
    #       HIGH-LEVEL FUNCTIONS
    # =======================================================

    def _Create(self):
        """
        Set up a new tournament on Lichess.
        """
        # Sanity check that we only do this when we should
        assert (self._SwissId is None), "Non-empty tournament object!"

        # Set up Lichess swiss tournament
        self._LichessCreate()



    def _WaitForStart(self):
        """
        Run a loop of listening to the Lichess API to see if we should be starting.
        If less than 10 seconds left, input pairings and block further joining.
        If maximum number of participants reached, input pairings, block further joining, and reduce start time to 10 seconds from now.
        """
        self.tprint("Waiting for start...")

        # NOTE: Keep track of participants even before starting,
        # to make sure early joiners will not be kicked out by late joiners when >maxparticipants at API check

        # Continuously listen for Lichess API if we want to start
        ReadyToStart = False
        while not ReadyToStart:

            # Stream Lichess list of participants via API and store in temporary variable
            self._UnconfirmedParticipants = dict()
            Lines = requests.get(f"https://lichess.org/api/swiss/{self._SwissId}/results",
                                 headers = {"Authorization": f"Bearer {self._LichessToken}"}).iter_lines()
            for Line in Lines:
                JUser = json.loads(Line.decode("utf-8"))
                self._UnconfirmedParticipants[JUser["username"].lower()] = JUser

            # Remove departed participants
            UsersToRemove = dict()
            for UserName, User in self._Participants.items():
                if UserName not in self._UnconfirmedParticipants:
                    self.tprint(f"Removing player {UserName}.")
                    UsersToRemove[UserName] = UserName

            # Remove users that left
            for UserName in UsersToRemove:
                self._Participants.pop(UserName)

            # Add newly registered participants
            for UserName, User in self._UnconfirmedParticipants.items():
                if UserName not in self._Participants:
                    self.tprint(f"Adding player {UserName}.")
                    self._Participants[UserName] = User

                    # If we reached the limit, stop registration and prepare to start the event
                    if len(self._Participants) >= self._MaxParticipants:

                        # Sanity checks on current parameters
                        assert (len(self._UnconfirmedParticipants) >= self._MaxParticipants), "How can this be if we reached the limit?"
                        assert (len(self._Participants) == self._MaxParticipants), "More than the limit?!"

                        self.tprint("Reached maximum participants!")

                        # Jump out of the loop to start the event
                        ReadyToStart = True
                        break

            # Do a further return for the above return case
            if ReadyToStart:
                break

            # Compute time left (in ms) until scheduled start
            TimeLeft = self._StartTime - 1000 * round(time.time())

            # Compute percentage of open spots left to register
            SpotsLeft = round(100 * (self._MaxParticipants - len(self._Participants)) / self._MaxParticipants)

            # Less than 15 seconds left: close participants, and head for start
            if TimeLeft < 15000:
                ReadyToStart = True
                break

            self.PrintParticipants()

            # Less than a minute left or less than 30% spots left: make API queries every 5 seconds
            if TimeLeft < 60000 or SpotsLeft < 30:
                self.tprint(f"Close to starting ({len(self._Participants)}/{self._MaxParticipants}), so sleeping for 5 seconds...")
                time.sleep(5)
            # Otherwise: make API queries every 10 seconds
            else:
                self.tprint(f"Not yet starting ({len(self._Participants)}/{self._MaxParticipants}), so sleeping for 10 seconds...")
                time.sleep(10)

        # EndWhile

        # If not enough participants, abort everything
        if len(self._Participants) < self._MinParticipants:
            r = requests.post(f"https://lichess.org/api/swiss/{self._SwissId}/terminate",
                  headers = {"Authorization": f"Bearer {self._LichessToken}"})
            self.tprint("Cancelled the tournament")
            sys.exit()

        else:
            self.tprint("Enough players to start!")

        # Sort participants by rating or randomize seeds
        if self._RandomizeSeeds:
            # Do random shuffle of seeds
            TempList = list(self._Participants.items())
            random.shuffle(TempList)
            self._Participants = dict(TempList)
        else:
            # Sort by rating
            self._Participants = dict(sorted(self._Participants.items(), key=lambda item: item[1]["rating"], reverse = True))

        # Assign seeds to participants
        for Seed, User in enumerate(self._Participants.values()):
            User["seed"] = Seed + 1

        self.PrintParticipants()

        # Reduce waiting time to start event
        TimeLeft = self._StartTime - 1000 * round(time.time())
        if TimeLeft > 15000:
            self._StartTime = 1000 * round(time.time()) + 15000
            Response = requests.post(f"https://lichess.org/api/swiss/{self._SwissId}/edit",
                                headers = {"Authorization": f"Bearer {self._LichessToken}"},
                                data = {"clock.limit": self._ClockInit,
                                        "clock.increment": self._ClockInc,
                                        "nbRounds": self._TotalRounds,
                                        "conditions.allowList": self._AllowedPlayers,
                                        "startsAt": self._StartTime})
            time.sleep(1)

        self.tprint("Finished waiting to start!")



    def _Start(self):
        """
        Actually start the tournament, once the time has expired or
        the number of participants has reached the limit.
        """
        self.tprint("Starting tournament...")

        # Finalize starting Lichess event
        self._LichessStart()

        # Update rounds if fewer participants than expected
        ActualMatchRounds = math.ceil(math.log2(len(self._Participants)))
        self._TreeSize = 2 ** ActualMatchRounds

        assert (len(self._Participants) <= self._TreeSize), "Tree size inconsistent! (too small)"
        assert (self._TreeSize < 2 * len(self._Participants)), "Tree size inconsistent! (too large)"

        if ActualMatchRounds < self._MatchRounds:
            self.tprint("Updating number of rounds on Lichess...")
            self._MatchRounds = ActualMatchRounds
            self._TotalRounds = self._MatchRounds * self._GamesPerMatch
            Response = requests.post(f"https://lichess.org/api/swiss/{self._SwissId}/edit",
                                     headers = {"Authorization": f"Bearer {self._LichessToken}"},
                                     data = {"clock.limit": self._ClockInit,
                                             "clock.increment": self._ClockInc,
                                             "nbRounds": self._TotalRounds,
                                             "conditions.allowList": self._AllowedPlayers})
            time.sleep(3)
            self.tprint("Finished updating API!")

        # Make bracket and save locally
        self.tprint("Making an empty bracket...")
        self._Bracket_MakeBracket(True)

        # Set flag accordingly
        self._Started = True

        self.tprint("Finished waiting for start (tournament almost started)!")



    def _StartMatches(self):
        """
        Preprocessing for match, such as making pairings.
        """
        self.tprint(f"Starting/preparing matches for match round {self._CurMatch+1}...")

        # Randomize who gets white/black first in these matches
        self._TopGetsWhite = random.randrange(0, 2)

        # First match round
        if self._CurMatch == 0:

            # Create initial list of participants from seed tree
            PairingList = []
            for i in range(self._TreeSize):

                # Get seed number
                t = trees.Trees[self._TreeSize][i] - 1

                # Store right player in PairingList
                ListPlayers = list(self._Participants.keys())
                if t < len(self._Participants):
                    PairingList.append([ListPlayers[t], False, []])
                else:
                    PairingList.append(["BYE", False, []])

            assert (len(PairingList) == self._TreeSize), "Weird pairing list error"

            # Store pairing list
            self._Pairings.append(PairingList)

        # Other match rounds
        else:

            # Create new pairings from previous results
            PairingList = []

            for i in range(len(self._Pairings[-1]) // 2):

                assert (self._Pairings[-1][2*i][1] != self._Pairings[-1][2*i+1][1]), "Previous pairings not complete!"

                # Pass winner to next round
                if self._Pairings[-1][2*i][1]:
                    PairingList.append([self._Pairings[-1][2*i][0], False, []])
                else:
                    PairingList.append([self._Pairings[-1][2*i+1][0], False, []])

            self._Pairings.append(PairingList)

        self.tprint(f"Finished starting/preparing match round {self._CurMatch+1}!")



    def _StartGames(self):
        """
        Start a new (sub)round.
        """
        self.tprint(f"Starting round {self._CurMatch+1}.{self._CurGame+1} ({self.GetRound()+1})...")

        # Do sanity check that pairings are ready
        assert (len(self._Pairings[-1]) >= 2), "No games to pair!"
        assert (len(self._Pairings[-1]) % 2 == 0), "Odd number of players to pair!"

        # Compute manual pairings to push to Lichess API
        PairingList = []
        for i in range(len(self._Pairings[-1]) // 2):

            # Extract players for this game
            Player1 = self._Pairings[-1][2*i][0]
            Player2 = self._Pairings[-1][2*i+1][0]

            # Identify white/black based on game number
            if self._CurGame % 2 == (1 - self._TopGetsWhite):
                # Swap order
                PlayerTemp = Player1
                Player1 = Player2
                Player2 = PlayerTemp

            # Store pairing in pairing list for API
            if Player1 == "BYE" or Player2 == "BYE":
                if Player1 == "BYE":
                    PairingList.append(f"{Player2} 1")
                else:
                    PairingList.append(f"{Player1} 1")
            else:
                PairingList.append(f"{Player1} {Player2}")

        self.PrintMatches()

        # Push the manual pairings to the API
        self._CurPairings = "\n".join(PairingList)
        self.tprint("Pushing pairings to API...")
        r = requests.post(f"https://lichess.org/api/swiss/{self._SwissId}/edit",
                          headers = {"Authorization": f"Bearer {self._LichessToken}"},
                          data = {"clock.limit": self._ClockInit,
                                  "clock.increment": self._ClockInc,
                                  "nbRounds": self._TotalRounds,
                                  "conditions.allowList": self._AllowedPlayers,
                                  "manualPairings": self._CurPairings})
        time.sleep(3)

        # Update game start time to 15 seconds from now
        NewRoundStartTime = 1000 * round(time.time()) + 15000
        if self.GetRound() == 0:
            # Tournament start, Lichess API endpoint .../edit
            requests.post(f"https://lichess.org/api/swiss/{self._SwissId}/edit",
                          headers = {"Authorization": f"Bearer {self._LichessToken}"},
                          data = {"clock.limit": self._ClockInit,
                                  "clock.increment": self._ClockInc,
                                  "nbRounds": self._TotalRounds,
                                  "conditions.allowList": self._AllowedPlayers,
                                  "startsAt": NewRoundStartTime})
            time.sleep(1)
        else:
            # New round start, Lichess API endpoint .../schedule-next-round
            requests.post(f"https://lichess.org/api/swiss/{self._SwissId}/schedule-next-round",
                          headers = {"Authorization": f"Bearer {self._LichessToken}"},
                          data = {"date": NewRoundStartTime})
            time.sleep(1)

        # Push the manual pairings to the API again to make sure
        self.tprint("Pushing pairings to API again...")
        r = requests.post(f"https://lichess.org/api/swiss/{self._SwissId}/edit",
                          headers = {"Authorization": f"Bearer {self._LichessToken}"},
                          data = {"clock.limit": self._ClockInit,
                                  "clock.increment": self._ClockInc,
                                  "nbRounds": self._TotalRounds,
                                  "conditions.allowList": self._AllowedPlayers,
                                  "manualPairings": self._CurPairings})
        time.sleep(3)

        # Update the bracket
        self.tprint(f"Updating the bracket...")
        self._Bracket_MakeBracket()

        self.tprint(f"Started round {self._CurMatch+1}.{self._CurGame+1} ({self.GetRound()+1})!")



    def _WaitForGamesToFinish(self):
        """
        Listen to API and wait for round to finish.
        """
        self.tprint(f"Waiting for round {self._CurMatch+1}.{self._CurGame+1} ({self.GetRound()+1}) to finish...")

        # Get Lichess API endpoint https://lichess.org/api/swiss/{id}
        # Check that "round": 13, and "nbOngoing": 0
        # If not, sleep for 10 seconds

        while True:
            # Get Lichess response how many games are running
            Response = requests.get(f"https://lichess.org/api/swiss/{self._SwissId}",
                             headers = {"Authorization": f"Bearer {self._LichessToken}"})
            JResponse = Response.json()

            if (JResponse["round"] == self.GetRound() + 1) and (JResponse["nbOngoing"] == 0):
                # Games have all finished
                break

            if (JResponse.get("status", "None") == "finished"):
                self.tprint("Tournament already finished early!")
                sys.exit()

            self.tprint("Sleeping (5), waiting for round to finish...")
            time.sleep(5)

        self.tprint(f"Finished waiting for round {self._CurMatch+1}.{self._CurGame+1} ({self.GetRound()+1}) to finish!")



    def _FinishGames(self):
        """
        Finish (sub)round, do post-processing.
        """
        self.tprint(f"Finishing round {self._CurMatch+1}.{self._CurGame+1} ({self.GetRound()+1})...")

        # Fetch user scores from Swiss event
        GameScores = dict()
        Lines = requests.get(f"https://lichess.org/api/swiss/{self._SwissId}/results",
                        headers = {"Authorization": f"Bearer {self._LichessToken}"}).iter_lines()
        for Line in Lines:
            JUser = json.loads(Line.decode("utf-8"))
            UserName = JUser["username"].lower()
            if UserName not in self._Participants:
                continue
            GameScores[UserName] = JUser["points"] - self._Participants[UserName]["points"]
            self._Participants[UserName]["points"] = JUser["points"]
        GameScores["BYE"] = 0

        time.sleep(3)

        # Process all matches one by one
        for i in range(len(self._Pairings[-1]) // 2):

            # Extract players for this game
            Player1 = self._Pairings[-1][2*i][0]
            Player2 = self._Pairings[-1][2*i+1][0]

            assert (GameScores[Player1] + GameScores[Player2] == 1), f"Error: {Player1} {GameScores[Player1]} - {GameScores[Player2]} {Player2}"

            # Store results in self._Pairings
            self._Pairings[-1][2*i][2].append(GameScores[Player1])
            self._Pairings[-1][2*i+1][2].append(GameScores[Player2])

        self.PrintMatches()

        self.tprint(f"Round {self._CurMatch+1}.{self._CurGame+1} ({self.GetRound()+1}) finished!")



    def _FinishMatches(self):
        """
        Do post-processing when a match round has finished.
        """
        self.tprint(f"Conclusing match round {self._CurMatch+1}...")

        # Process all matches one by one
        for i in range(len(self._Pairings[-1]) // 2):

            # Extract players for this game
            Player1 = self._Pairings[-1][2*i][0]
            Player2 = self._Pairings[-1][2*i+1][0]
            Score1 = sum(self._Pairings[-1][2*i][2])
            Score2 = sum(self._Pairings[-1][2*i+1][2])
            # Player 1 won outright

            if Score1 > Score2:
                Player1Won = True

            # Player 2 won outright
            elif Score1 < Score2:
                Player1Won = False

            # Tiebreak deciding
            else:

                # Two possible methods: by rating, and by color
                if self._TieBreak == "rating":
                    # Lower-rated player wins
                    if self._Participants[Player1]["rating"] <= self._Participants[Player2]["rating"]:
                        Player1Won = True
                    else:
                        Player1Won = False
                else:
                    # Determine winner by color
                    if self._TopGetsWhite:
                        Player1Won = False
                    else:
                        Player1Won = True


            # Store match results in self._Pairings
            self._Pairings[-1][2*i][1] = Player1Won
            self._Pairings[-1][2*i+1][1] = not Player1Won

        self.PrintMatches()

        # Update the bracket
        self._Bracket_MakeBracket()

        self.tprint(f"Finished concluding match round {self._CurMatch+1}!")



    def _Finalize(self):
        """
        Wrap up and finalize the tournament.
        """
        self.tprint("Finalizing the tournament...")

        # Wrap up winners/losers
        if self._Pairings[-1][0][1]:
            self._Winner = self._Pairings[-1][0][0]
            self._Loser = self._Pairings[-1][1][0]
        else:
            self._Winner = self._Pairings[-1][1][0]
            self._Loser = self._Pairings[-1][0][0]
        self.tprint(f"Winner: {self._Winner}")

        # Update the final bracket
        self._Bracket_MakeBracket()

        EndMessage = f"Thanks to those who played in the event! Congrats to the winner {self._Winner}!"
        # f"https://lichess.org/team/{self._TeamId}/pm-all"

        self.tprint("Finished finalizing the tournament!")



    def MainLoop(self):
        """
        The main loop for creating and running a tournament.
        This fully automates the process of hosting a knock-out tournament,
        making sure pairings are done according to the knock-out bracket.
        """
        self._Create()                              # Create tournament on Lichess
        self._WaitForStart()                        # Repeatedly listen for updates on API until start
        self._Start()                               # Formally start the tournament
        for m in range(self._MatchRounds):          # For each round of matches:
            self._CurMatch = m                      # Update current match counter
            self._CurGame = -1                      # Update current game counter
            self._StartMatches()                    # Preprocessing for starting matches, e.g., getting pairings
            for g in range(self._GamesPerMatch):    # For each game round within a match round
                self._CurGame = g                   # Update current game counter
                self._StartGames()                  # Preprocessing for starting games, e.g., pushing pairings
                self._WaitForGamesToFinish()        # Loop and wait for all games to end
                self._FinishGames()                 # Update match results via Lichess API results
            self._FinishMatches()                   # Last games have finished, decide winners/tiebreaks
        self._Finalize()                            # Finalize Lichess event
