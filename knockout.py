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

    # Class variables, independent of the object instance
    # Bracket drawing: foreground colors
    _Bracket_ColorName          = (186/255, 186/255, 186/255)
    _Bracket_ColorNameWinner    = (220/255, 220/255, 220/255)
    _Bracket_ColorNameLoser     = (150/255, 150/255, 150/255)
    _Bracket_ColorURL           = (100/255, 100/255, 100/255)
    _Bracket_ColorWin           = ( 98/255, 153/255,  36/255)
    _Bracket_ColorLoss          = (204/255,  51/255,  51/255)
    _Bracket_ColorLossGame      = ( 71/255,  39/255,  36/255)
    _Bracket_ColorGold          = (255/255, 203/255,  55/255)
    _Bracket_ColorSilver        = (207/255, 194/255, 170/255)
    _Bracket_ColorDraw          = (123/255, 153/255, 153/255)
    _Bracket_ColorArrow         = ( 60/255,  60/255,  60/255)

    # Background colors
    _Bracket_ColorBGAll         = ( 22/255,  21/255,  18/255)
    _Bracket_ColorBGScoreBlack  = ( 33/255,  31/255,  28/255)
    _Bracket_ColorBGScoreWhite  = ( 43/255,  41/255,  38/255)
    _Bracket_ColorBGName        = ( 58/255,  56/255,  51/255)

    # API request parameters
    _ApiDelay                   = 3       # Wait 3 seconds between API requests
    _ApiAttempts                = 5       # Retry 5 times at API endpoints before giving up

    def __init__(self, LichessToken, GitHubToken, ConfigFile):
        """
        Initialize a new KO tournament object.
        """
        # Store arguments in object
        self._ConfigFile        = ConfigFile
        self._LichessToken      = LichessToken
        self._GitHubToken       = GitHubToken

        # Load configuration variables
        Config                  = configparser.ConfigParser(inline_comment_prefixes = ";")
        Config.read(self._ConfigFile)
        self._GitHubUserName    = Config["GitHub"]["Username"].strip()
        self._GitHubRepoName    = Config["GitHub"]["Repository"].strip()
        self._TeamId            = Config["Lichess"]["TeamId"].strip()
        self._MaxParticipants   = int(Config["Options"]["MaxParticipants"])
        self._MinParticipants   = int(Config["Options"]["MinParticipants"])
        self._StartAtMax        = Config["Options"].getboolean("StartAtMax")
        self._GamesPerMatch     = int(Config["Options"]["GamesPerMatch"])
        self._Rated             = Config["Options"].getboolean("Rated")
        self._Variant           = Config["Options"]["Variant"].strip()
        self._ClockInit         = int(Config["Options"]["ClockInit"])
        self._ClockInc          = int(Config["Options"]["ClockInc"])
        self._ChatFor           = int(Config["Options"]["ChatFor"])
        self._RandomizeSeeds    = Config["Options"].getboolean("RandomizeSeeds")
        self._Title             = Config["Options"]["EventName"].strip()
        self._MinutesToStart    = int(Config["Options"]["MinutesToStart"])
        self._TieBreak          = Config["Options"]["TieBreak"]

        # Check validity of input data, rule out PEBKAC
        self._ValidateInput()

        # Variables which should not be modified
        self._SwissId           = None
        self._SwissUrl          = None
        self._Winner            = None
        self._Loser             = None
        self._MatchRounds       = math.ceil(math.log2(self._MaxParticipants))
        self._TotalRounds       = self._GamesPerMatch * self._MatchRounds
        self._TreeSize          = 2 ** self._MatchRounds                   # If e.g. 5 players, it is 8

        # Decide white/black in initial games in each match
        # - Value 0 gives bottom player in bracket white first
        # - Value 1 gives top player in bracket white first
        self._TopGetsWhite      = [random.randrange(0, 2) for _ in range(self._MatchRounds)]
        self.tprint(self._TopGetsWhite)

        self._Description       = f"Knock-out tournament for up to {self._MaxParticipants} players. "
        self._Description      += f"Each match consists of {self._GamesPerMatch} game{'s' if (self._GamesPerMatch > 1) else ''}. "
        self._Description      += f"Registration closes 30 seconds before the start. "
        if self._TieBreak == "color":
            self._Description  += "In case of a tie, the player with more black games advances. "
        else: # if self._TieBreak == "rating":
            self._Description  += "In case of a tie, the lower-rated player advances. "
        self._Started           = False
        self._UnconfirmedParticipants = dict()  # The players registered on Lichess
        self._Participants      = dict()        # The players registered, confirmed to play, with scores
        self._AllowedPlayers    = ""
        self._CurGame           = -1
        self._CurMatch          = -1

        assert (self._TotalRounds >= 3), "Parameters indicate not enough rounds"
        assert (self._TotalRounds <= 100), "Parameters indicate too many rounds"

        # List of lists of implicit pairings
        # Pairings = [[(A, True, [1, 1, 0, 1]), (B, False, [0, 0, 1, 0]), C, D, E, F, G, H], [A, D*, F*, H], [D*, F]]
        # Means:      [A-B,  C-D,  E-F,  G-H]   [A-D,  F-H]   [D-F]
        # Based on tree pairings from trees.py
        # Add * to show a player advances
        self._Pairings          = []
        self._CurPairings       = None        # API string to pass to Lichess

        # Start at specified in configuration, rounded to multiple of 10 minutes
        self._StartTime         = 1000 * round(time.time()) + 60 * 1000 * self._MinutesToStart
        self._StartTime         = 600000 * (self._StartTime // 600000)     # Round to multiple of 10 minutes

        # Create a bracket image folder, if it does not exist
        if not os.path.exists("png"):
            os.makedirs("png")
        if not os.path.exists("logs"):
            os.makedirs("logs")

        # No file name known yet, so no log yet
        self._LogFile           = None

        # Set up a github authentication workflow
        while True:
            try:
                auth = github.Auth.Token(self._GitHubToken)
                g = github.Github(auth = auth)
                self._GitHubRepo = g.get_user().get_repo(self._GitHubRepoName)
                break
            except:
                self.tprint("Failing to connect to GitHub. Retrying...")
                time.sleep(self._ApiDelay)



    # =======================================================
    #       Validating all user input before start
    # =======================================================

    def _ValidateInput(self):
        """
        Do checks on the user-provided input, to make sure we can get started.
        """
        self.tprint("Start validating user input...")

        # Convenient for checking validity of various strings
        alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        numeric = "0123456789"

        # GitHub username
        NameChars = alphabet + numeric + ",.-_"
        NameLength = 39
        assert (all([x in NameChars for x in self._GitHubUserName])), "Invalid GitHub username"
        assert (len(self._GitHubUserName) <= NameLength), "GitHub username too long"
        assert (len(self._GitHubUserName) >= 1), "GitHub username too short"

        # GitHub repository
        RepoChars = alphabet + numeric + ".-_"
        assert (all([(x in RepoChars) for x in self._GitHubRepoName])), "Invalid GitHub repository"
        RepoEndpoint = f"https://api.github.com/repos/{self._GitHubUserName}/{self._GitHubRepoName}"
        Response = self._RunGetRequest(RepoEndpoint, False, AuthorizeLichess=False, AuthorizeGitHub=True)
        JResponse = Response.json()
        assert ("id" in JResponse), "GitHub repository not found"
        assert ("permissions" in JResponse), "GitHub token invalid"
        assert (JResponse["permissions"].get("push", False)), "GitHub token does not permit pushing"

        # Lichess user token
        TokenEndpoint = "https://lichess.org/api/token/test"
        Response = self._RunPostRequest(TokenEndpoint, self._LichessToken)
        JResponse = Response.json()
        assert (self._LichessToken in JResponse), "Invalid Lichess token"
        assert ("tournament:write" in JResponse[self._LichessToken]["scopes"]), "Incorrect Lichess token scopes"
        self._LichessUsername = JResponse[self._LichessToken].get("userId")

        # Lichess team name
        TeamChars = alphabet + numeric + "-"
        assert (all([x in TeamChars for x in self._TeamId])), "Invalid Lichess team ID"
        TeamEndpoint = f"https://lichess.org/api/team/{self._TeamId}"
        Response = self._RunGetRequest(TeamEndpoint, False, True)
        TeamResponse = Response.json()
        assert ("id" in TeamResponse), "Lichess team not found"
        # Cannot check if user is team leader, as it can be hidden

        # Event name
        EventChars = alphabet + numeric + " ,.-"
        assert (all([x.lower() in EventChars for x in self._Title])), "Illegal event name"
        assert ((len(self._Title) in range(2, 31)) or (self._Title == "")), "Event name has improper length"

        # Tiebreak criterium
        assert (self._TieBreak in {"rating", "color"}), "No proper tiebreak specified"
        assert ((self._TieBreak != "color") or (self._GamesPerMatch % 2 == 1)), "Deciding winner by color only possible for odd match lengths"

        # Randomizing seeds
        assert (self._RandomizeSeeds in {True, False}), "RandomizeSeeds not a boolean value"

        # Minutes to start
        assert (self._MinutesToStart >= 5), "Minutes to start too short (less than 5 minutes)"
        assert (self._MinutesToStart <= 60 * 24 * 7), "Minutes to start too long (more than a week)"

        # Minimum, maximum participants
        assert (self._MinParticipants >= 4), "Minimum number of participants too low (must be at least 4)"
        assert (self._MinParticipants <= self._MaxParticipants), "Minimum number of participants higher than maximum"
        assert (self._MaxParticipants <= 8192), "Maximum number of participants too high (must be at most 8192)"

        # Starting when the participant limit has been reached
        assert (self._StartAtMax in {True, False}), "StartAtMax not a boolean value"

        # Games per match
        assert (self._GamesPerMatch >= 1), "Need at least 1 game per match"
        assert (self._GamesPerMatch <= 20), "Too many games per match"

        # Rated or not
        assert (self._Rated in {True, False}), "Rated not a boolean value"

        # Time control
        AllowedInit = {0, 15, 30, 45, 60, 90, 120, 180, 240, 300, 360, 420, 480, 600,
                       900, 1200, 1500, 1800, 2400, 3000, 3600, 4200, 4800, 5400, 6000,
                       6600, 7200, 7800, 8400, 9000, 9600, 10200, 10800}
        assert (self._ClockInit in AllowedInit), "Invalid initial clock time"
        assert (self._ClockInc >= 0), "Invalid increment time"
        assert (self._ClockInc <= 120), "Increment too high (above 1 minute per move)"
        # Specific restrictions from https://lichess.org/api#tag/Swiss-tournaments/operation/apiSwissNew
        assert (self._ClockInit + self._ClockInc > 0), "Cannot play 0+0"
        assert (((self._ClockInit, self._ClockInc) not in {(0, 1), (15, 0)})
                or (self._Variant == "standard")
                or (not self._Rated)), "Degenerate variant time controls cannot be rated"

        # Chess variant
        AllVariants = {"standard", "chess960", "crazyhouse", "antichess", "atomic", "horde",
                       "kingOfTheHill", "racingKings", "threeCheck", "fromPosition"}
        assert (self._Variant in AllVariants), "Improper chess variant specified"

        # Chat settings
        assert (self._ChatFor in {0, 10, 20, 30}), "Improper ChatFor parameter (must be 0/10/20/30)"

        self.tprint("Finished validating user input!")



    # =======================================================
    #       Internal calculations
    # =======================================================

    def _GetRound(self) -> int:
        return self._CurMatch * self._GamesPerMatch + self._CurGame



    def _BracketFile(self) -> str:
        return f"png{os.sep}{self._SwissId}.png"



    # =======================================================
    #       Logging functions
    # =======================================================

    def tprint(self, s):
        ToPrint = f"{datetime.datetime.now().strftime('%H:%M:%S')}: {s}"
        print(ToPrint)
        if hasattr(self, "_LogFile") and (self._LogFile is not None):
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



    # =======================================================
    #       API request handling
    # =======================================================

    def _RunGetRequest(self, RequestEndpoint: str, KillOnFail: bool, AuthorizeLichess: bool = True, AuthorizeGitHub: bool = False):
        """
        Run an API request, and handle potential errors.
        If the flag KillOnFail is true, the tournament will be aborted
        if no proper response is obtained from the server.
        """
        self.tprint(f"GET-request to {RequestEndpoint}.")

        # Try to run the request a number of times
        RequestSuccess = False
        for i in range(self._ApiAttempts):
            try:
                if AuthorizeLichess:
                    Response = requests.get(RequestEndpoint,
                                headers = {"Authorization": f"Bearer {self._LichessToken}"})
                elif AuthorizeGitHub:
                    Response = requests.get(RequestEndpoint,
                                headers = {"Authorization": f"Bearer {self._GitHubToken}"})
                else:
                    Response = requests.get(RequestEndpoint)
                Response.raise_for_status()
                RequestSuccess = True
                break
            except:
                self.tprint(f"GET-request at {RequestEndpoint} failed!")
                self.tprint(f"Attempt {i+1}/{self._ApiAttempts}. {f'Trying again in {self._ApiDelay} seconds...' if i < self._ApiAttempts - 1 else ''}")
                time.sleep(self._ApiDelay)

        # Exit if we did not succeed creating a tournament
        if not RequestSuccess:
            self.tprint(f"Unable to process GET-request!")
            if KillOnFail:
                self._KillTournament()
            self.tprint("Goodbye!")
            sys.exit()

        # Return response if everything worked successfully
        self.tprint(f"GET-request succeeded! Continuing in {self._ApiDelay} seconds...")
        time.sleep(self._ApiDelay)
        return Response


    def _RunPostRequest(self, RequestEndpoint: str, RequestData, KillOnFail: bool = False):
        """
        Run an API request, and handle potential errors.
        If the flag KillOnFail is set to true, the tournament will be aborted
        if no proper response is obtained from the server.
        """
        self.tprint(f"POST-request to {RequestEndpoint}.")

        # Try to run the request a number of times
        RequestSuccess = False
        for i in range(self._ApiAttempts):
            try:
                Response = requests.post(RequestEndpoint,
                                headers = {"Authorization": f"Bearer {self._LichessToken}"},
                                data = RequestData)
                Response.raise_for_status()
                RequestSuccess = True
                break
            except:
                self.tprint(f"POST-request at {RequestEndpoint} failed!")
                self.tprint(Response)
                self.tprint(Response.content)
                self.tprint(f"Attempt {i+1}/{self._ApiAttempts}. {f'Trying again in {self._ApiDelay} seconds...' if i < self._ApiAttempts - 1 else ''}")
                time.sleep(self._ApiDelay)

        # Exit if we did not succeed creating a tournament
        if not RequestSuccess:
            self.tprint(f"Unable to process POST-request!")
            if KillOnFail:
                self._KillTournament()
            self.tprint("Goodbye!")
            sys.exit()

        # Return response if everything worked successfully
        self.tprint(f"POST-request succeeded! Continuing in {self._ApiDelay} seconds...")
        time.sleep(self._ApiDelay)
        return Response


    def _KillTournament(self):
        """
        In case we fail to connect to the server, we abort the tournament.
        """
        self.tprint("Attempting to cancel the tournament...")
        RequestEndpoint = f"https://lichess.org/api/swiss/{self._SwissId}/terminate"
        RequestData = dict()
        self._RunPostRequest(RequestEndpoint, RequestData, False)
        self.tprint("Successfully canceled the tournament")



    # =======================================================
    #       Bracket visualization
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
        plt.figure()
        plt.style.use(['dark_background'])
        self._fig, self._ax = plt.subplots(figsize=(self._Xtotal/2,self._Ytotal/2))
        self._fig.patch.set_facecolor(self._Bracket_ColorBGAll)

        # Temporary, remove later
        self._userchars = "abcdefghijklmnopqrstuvwxyz0123456789-_"

        # List of display information depending on win/loss/draw
        self._Bracket_DisplayScores = []
        self._Bracket_DisplayScores.append({
            "Color": self._Bracket_ColorLoss,
            "ColorGame": self._Bracket_ColorLossGame,
            "Weight": "normal",
            "WeightGame": "bold"})
        self._Bracket_DisplayScores.append({
            "Color": self._Bracket_ColorDraw,
            "ColorGame": self._Bracket_ColorDraw,
            "Weight": "normal",
            "WeightGame": "bold"})
        self._Bracket_DisplayScores.append({
            "Color": self._Bracket_ColorWin,
            "ColorGame": self._Bracket_ColorWin,
            "Weight": "bold",
            "WeightGame": "bold"})



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
        self._ax.add_patch(mpl.patches.Rectangle((XBase + self._Xn, YBase),
                     self._GamesPerMatch * self._Xg + 0.2,
                     self._Yh,
                     facecolor = self._Bracket_ColorBGScoreBlack,
                     fill = True,
                     lw = 0))

        # White/black: alternating shading to indicate who was white
        for g in range(self._GamesPerMatch):
            self._ax.add_patch(mpl.patches.Rectangle((XBase + self._Xn + g * self._Xg + (0.1 if g > 0 else 0),
                        YBase + ((g + 1 + self._TopGetsWhite[r]) % 2) * self._Yh / 2),
                        self._Xg + (0.1 if (g == 0) else 0) + (0.1 if (g == self._GamesPerMatch - 1) else 0),
                        self._Yh / 2,
                        facecolor = self._Bracket_ColorBGScoreWhite,
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



    def _Bracket_DrawURL(self):
        """
        Add the URL to the tournament in the bottom right corner.
        """
        plt.text((self._Xn + self._GamesPerMatch * self._Xg)/2 + (round(math.log2(self._TreeSize)) - 1) * self._Xw,
            0.2,
            f"https://lichess.org/swiss/{self._SwissId}",
            fontsize = 12,
            ha = "center",
            va = "center",
            color = self._Bracket_ColorURL)



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
                     self._Ytotal - 0.7,
                     RoundNames[MatchesLeft],
                     fontsize = 20,
                     fontweight = "bold",
                     ha = "center",
                     va = "center",
                     color = self._Bracket_ColorName)
            if self._GamesPerMatch == 1:
                RoundText = f"(Round {r * self._GamesPerMatch + 1})"
            else:
                RoundText = f"(Rounds {r * self._GamesPerMatch + 1}-{(r + 1) * self._GamesPerMatch})"
            plt.text((self._Xn + self._GamesPerMatch * self._Xg)/2 + r * self._Xw,
                     self._Ytotal - 1.4,
                     RoundText,
                     fontsize = 14,
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
        if User1Won == 2:
            MatchUserNameColor = [self._Bracket_ColorNameWinner, self._Bracket_ColorNameLoser]
        elif User2Won == 2:
            MatchUserNameColor = [self._Bracket_ColorNameLoser, self._Bracket_ColorNameWinner]
        else:
            MatchUserNameColor = [self._Bracket_ColorName, self._Bracket_ColorName]

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
                     color = MatchUserNameColor[j])

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
                         fontweight = self._Bracket_DisplayScores[round(2*MatchUserScores[j][g])]["WeightGame"],
                         ha = "center",
                         va = "center",
                         color = self._Bracket_DisplayScores[round(2*MatchUserScores[j][g])]["ColorGame"])



    def _Bracket_DrawWinners(self):
        """
        Draw trophies and add names of winners/losers.
        Only if the tournament finished, and only if
        the tournament bracket is bigger than 4 players.
        """
        assert (self._CurMatch == self._MatchRounds - 1), "Not in the last match yet."
        assert (self._Winner is not None), "No winner yet."
        assert (self._Loser is not None), "No loser yet."

        # Two cases: more than 4 players (with trophy icon), and 4 players (just text)
        if self._TreeSize > 4:
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
                        color = self._Bracket_ColorGold)

            # Show finals loser with trophy
            im2 = f"trophies/lichess-silver.png"
            img2 = plt.imread(im2)
            imgar2 = 1.0
            Xmin = (self._Xn + self._GamesPerMatch * self._Xg)/2 + (self._MatchRounds - 1) * self._Xw
            Xmin = Xmin - 1
            Xmax = Xmin + 2
            Ymin = self._Ytotal / 2 - 4.5
            Ymax = Ymin + (Xmax - Xmin) / imgar2
            plt.imshow(img2, extent = (Xmin, Xmax, Ymin, Ymax))
            plt.text(Xmin + 1,
                    Ymin - 0.5,
                    self._Participants[self._Loser]["username"],
                        fontsize = 16,
                        fontweight = "bold",
                        ha = "center",
                        va = "center",
                        color = self._Bracket_ColorSilver)
        # Only 4 players in event
        else:
            Xmin = (self._Xn + self._GamesPerMatch * self._Xg)/2 + (self._MatchRounds - 1) * self._Xw
            Xmin = Xmin - 1.5
            Ymin = self._Ytotal / 2 + 1.3
            plt.text(Xmin + 1.5,
                    Ymin - 0.5,
                    self._Participants[self._Winner]["username"] + " won!",
                        fontsize = 18,
                        fontweight = "bold",
                        ha = "center",
                        va = "center",
                        color = self._Bracket_ColorGold)

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
        self._Bracket_DrawURL()



    def _Bracket_FillScheme(self):
        """
        Based on pairing data, fill scheme with data and results.
        """
        # If proper pairings exist
        if self._Pairings == []:

            # Before start of event, make pretend bracket
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

            # After pairings have been finalized, do things properly
            for r in range(len(self._Pairings)):
                for i in range(self._TreeSize // (2 ** (r + 1))):
                    self._Bracket_FillMatchBlock(r, i)

            # Clear pairings again
            self._Pairings = []

        else:
            # After pairings have been finalized, do things properly
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
        plt.savefig(self._BracketFile())
        plt.cla()
        plt.close("all")



    def _Bracket_Upload(self, New = False):
        """
        Once the bracket image has been generated, upload it.
        """
        # Load contents to upload
        with open(self._BracketFile(), "rb") as file:
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



    def _Bracket_MakeBracket(self):
        """
        Main routine for drawing a bracket.
        """
        New = True
        if os.path.exists(self._BracketFile()):
            New = False
        self._Bracket_Initialize()
        self._Bracket_DrawEmptyScheme()
        self._Bracket_FillScheme()
        if self._Winner is not None:
            self._Bracket_DrawWinners()
        self._Bracket_Save()
        self._Bracket_Upload(New)



    # =======================================================
    #       High-level functions
    # =======================================================

    def _Create(self):
        """
        Set up a new tournament on Lichess.
        """
        # Sanity check that we only do this when we should
        assert (self._SwissId is None), "Non-empty tournament object!"

        # Set up Lichess swiss tournament
        self.tprint("Creating new Lichess Swiss tournament...")

        # Create Lichess Swiss tournament with error handling
        RequestEndpoint = f"https://lichess.org/api/swiss/new/{self._TeamId}"
        RequestData = dict()
        RequestData["name"]             = self._Title
        RequestData["clock.limit"]      = self._ClockInit
        RequestData["clock.increment"]  = self._ClockInc
        RequestData["nbRounds"]         = self._TotalRounds
        RequestData["startsAt"]         = self._StartTime
        RequestData["roundInterval"]    = 99999999
        RequestData["variant"]          = self._Variant
        RequestData["description"]      = self._Description
        RequestData["rated"]            = ("true" if self._Rated else "false")
        RequestData["chatFor"]          = self._ChatFor
        Response = self._RunPostRequest(RequestEndpoint, RequestData, False)

        # At this point we know the request succeeded, so we can continue
        self.tprint("Tournament creation succeeded!")
        JResponse = Response.json()

        # Store some data in the object
        self._SwissId = JResponse["id"]
        self._SwissUrl = f"https://lichess.org/swiss/{self._SwissId}"
        self._LogFile = open(f"logs{os.sep}{self._SwissId}.txt", "w")

        self.tprint("Opened a new log file.")

        # Update the tournament description with bracket URL
        if (self._GitHubUserName == "tmmlaarhoven") and (self._GitHubRepoName == "lichess-knockout"):
            # Custom short URL on my own domain
            self._Description += f"\n\nPairings: https://ko.thijs.com/png/{self._SwissId}.png"
        else:
            # Otherwise the standard GitHub location where the file is hosted
            self._Description += f"\n\nPairings: https://raw.githubusercontent.com/{self._GitHubUserName}/{self._GitHubRepoName}/main/png/{self._SwissId}.png"

        # Attempt to push update to server - at most 5 attempts
        RequestEndpoint = f"https://lichess.org/api/swiss/{self._SwissId}/edit"
        RequestData = dict()
        RequestData["clock.limit"]      = self._ClockInit
        RequestData["clock.increment"]  = self._ClockInc
        RequestData["nbRounds"]         = self._TotalRounds
        RequestData["description"]      = self._Description
        Response = self._RunPostRequest(RequestEndpoint, RequestData, True)

        # At this point we know that the update also succeeded
        self.tprint(f"Finished creating a new Lichess swiss tournament!")
        self.tprint(f"Tournament available at {self._SwissUrl}.")



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
            RequestEndpoint = f"https://lichess.org/api/swiss/{self._SwissId}/results"
            Response = self._RunGetRequest(RequestEndpoint, True, True)
            Lines = Response.iter_lines()
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

            # Add newly registered participants, if there is place
            for UserName, User in self._UnconfirmedParticipants.items():
                if len(self._Participants) == self._MaxParticipants:
                    break

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
                        if self._StartAtMax:
                            ReadyToStart = True

                        # Stop adding more players
                        break

            # Do a further return in case we wish to start early
            if ReadyToStart:
                break

            # Compute time left (in ms) until scheduled start
            TimeLeft = self._StartTime - 1000 * round(time.time())

            # Compute percentage of open spots left to register
            SpotsLeft = round(100 * (self._MaxParticipants - len(self._Participants)) / self._MaxParticipants)

            # Less than 30 seconds left: close participants, and head for start
            if TimeLeft < 30000:
                ReadyToStart = True
                break

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

            # Make bracket with current participants
            self.tprint("Making a preliminary bracket...")
            self._Bracket_MakeBracket()

            # Less than a minute left or less than 30% spots left: make API queries every few seconds
            if (TimeLeft < 60000) or ((self._StartAtMax) and (SpotsLeft < 30)):
                self.tprint(f"Close to starting ({len(self._Participants)}/{self._MaxParticipants} participants)...")
                # time.sleep(self._ApiDelay)
            # Otherwise: make API queries every 10 seconds
            else:
                self.tprint(f"Not yet starting ({len(self._Participants)}/{self._MaxParticipants}), so sleeping for another 10 seconds...")
                time.sleep(10)

        # EndWhile

        # If not enough participants, abort everything
        if len(self._Participants) < self._MinParticipants:
            self._KillTournament()
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

        # Reduce waiting time to start event in at most 30 seconds
        TimeLeft = self._StartTime - 1000 * round(time.time())
        if TimeLeft > 30000:
            self._StartTime = 1000 * round(time.time()) + 30000
            ResponseEndpoint = f"https://lichess.org/api/swiss/{self._SwissId}/edit"
            ResponseData = dict()
            ResponseData["clock.limit"]             = self._ClockInit
            ResponseData["clock.increment"]         = self._ClockInc
            ResponseData["nbRounds"]                = self._TotalRounds
            ResponseData["conditions.allowList"]    = self._AllowedPlayers
            ResponseData["startsAt"]                = self._StartTime
            Response = self._RunPostRequest(ResponseEndpoint, ResponseData, True)

        self.tprint("Finished waiting to start!")



    def _Start(self):
        """
        Actually start the tournament, once the time has expired or
        the number of participants has reached the limit.
        """
        self.tprint("Starting tournament...")

        # Set list of allowed participants in API to current list of participants
        self._AllowedPlayers = "\n".join(self._Participants.keys())

        # Prepare proper post request
        RequestEndpoint = f"https://lichess.org/api/swiss/{self._SwissId}/edit"
        RequestData = dict()
        RequestData["clock.limit"]          = self._ClockInit
        RequestData["clock.increment"]      = self._ClockInc
        RequestData["nbRounds"]             = self._TotalRounds
        RequestData["conditions.allowList"] = self._AllowedPlayers
        _ = self._RunPostRequest(RequestEndpoint, RequestData, True)

        # Message players who were left out
        for UserName in self._UnconfirmedParticipants:
            if UserName not in self._Participants:
                self.tprint(f"Sorry {UserName}, you were too late!")

        # Update rounds if fewer participants than expected
        ActualMatchRounds = math.ceil(math.log2(len(self._Participants)))
        self._TreeSize = 2 ** ActualMatchRounds

        assert (len(self._Participants) <= self._TreeSize), "Tree size inconsistent! (too small)"
        assert (self._TreeSize < 2 * len(self._Participants)), "Tree size inconsistent! (too large)"

        if ActualMatchRounds < self._MatchRounds:
            self.tprint("Updating number of rounds on Lichess...")
            self._MatchRounds = ActualMatchRounds
            self._TotalRounds = self._MatchRounds * self._GamesPerMatch
            ResponseEndpoint = f"https://lichess.org/api/swiss/{self._SwissId}/edit"
            ResponseData = dict()
            ResponseData["clock.limit"]             = self._ClockInit
            ResponseData["clock.increment"]         = self._ClockInc
            ResponseData["nbRounds"]                = self._TotalRounds
            ResponseData["conditions.allowList"]    = self._AllowedPlayers
            self._RunPostRequest(ResponseEndpoint, ResponseData, True)
            self.tprint("Finished updating API!")

        # Make bracket and save locally
        self.tprint("Making the complete bracket...")
        self._Bracket_MakeBracket()

        # Set flag accordingly
        self._Started = True

        self.tprint("Finished waiting for start (tournament almost started)!")



    def _StartMatches(self):
        """
        Preprocessing for match, such as making pairings.
        """
        self.tprint(f"Starting/preparing matches for match round {self._CurMatch+1}...")

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
        self.tprint(f"Starting round {self._CurMatch+1}.{self._CurGame+1} ({self._GetRound()+1})...")

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
            if self._CurGame % 2 == (1 - self._TopGetsWhite[self._CurMatch]):
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
        RequestEndpoint = f"https://lichess.org/api/swiss/{self._SwissId}/edit"
        RequestData = dict()
        RequestData["clock.limit"]          = self._ClockInit
        RequestData["clock.increment"]      = self._ClockInc
        RequestData["nbRounds"]             = self._TotalRounds
        RequestData["conditions.allowList"] = self._AllowedPlayers
        RequestData["manualPairings"]       = self._CurPairings
        _ = self._RunPostRequest(RequestEndpoint, RequestData, True)

        # Update game start time to 15 seconds from now
        NewRoundStartTime = 1000 * round(time.time()) + 15000
        if self._GetRound() == 0:
            # Tournament start, Lichess API endpoint .../edit
            RequestEndpoint = f"https://lichess.org/api/swiss/{self._SwissId}/edit"
            RequestData = dict()
            RequestData["clock.limit"]          = self._ClockInit
            RequestData["clock.increment"]      = self._ClockInc
            RequestData["nbRounds"]             = self._TotalRounds
            RequestData["conditions.allowList"] = self._AllowedPlayers
            RequestData["manualPairings"]       = self._CurPairings
            RequestData["startsAt"]             = NewRoundStartTime
            _ = self._RunPostRequest(RequestEndpoint, RequestData, True)
        else:
            # New round start, Lichess API endpoint .../schedule-next-round
            RequestEndpoint = f"https://lichess.org/api/swiss/{self._SwissId}/schedule-next-round"
            RequestData = dict()
            RequestData["date"]                 = NewRoundStartTime
            _ = self._RunPostRequest(RequestEndpoint, RequestData, True)

        # Push the manual pairings to the API again to make sure
        self.tprint("Pushing pairings to API again...")
        RequestEndpoint = f"https://lichess.org/api/swiss/{self._SwissId}/edit"
        RequestData = dict()
        RequestData["clock.limit"]          = self._ClockInit
        RequestData["clock.increment"]      = self._ClockInc
        RequestData["nbRounds"]             = self._TotalRounds
        RequestData["conditions.allowList"] = self._AllowedPlayers
        RequestData["manualPairings"]       = self._CurPairings
        _ = self._RunPostRequest(RequestEndpoint, RequestData, True)

        # Update the bracket
        self.tprint(f"Updating the bracket...")
        self._Bracket_MakeBracket()

        self.tprint(f"Started round {self._CurMatch+1}.{self._CurGame+1} ({self._GetRound()+1})!")



    def _WaitForGamesToFinish(self):
        """
        Listen to API and wait for round to finish.
        """
        self.tprint(f"Waiting for round {self._CurMatch+1}.{self._CurGame+1} ({self._GetRound()+1}) to finish...")

        # Get Lichess API endpoint https://lichess.org/api/swiss/{id}
        # Check that "round": 13, and "nbOngoing": 0
        # If not, sleep for 10 seconds

        while True:
            # Get Lichess response how many games are running
            RequestEndpoint = f"https://lichess.org/api/swiss/{self._SwissId}"
            Response = self._RunGetRequest(RequestEndpoint, True, True)
            JResponse = Response.json()

            if (JResponse["round"] == self._GetRound() + 1) and (JResponse["nbOngoing"] == 0):
                # Games have all finished
                break

            if (JResponse.get("status", "None") == "finished"):
                self.tprint("Tournament already finished early!")
                sys.exit()

            self.tprint(f"Waiting for round to finish...")
            # time.sleep(self._ApiDelay)

        self.tprint(f"Finished waiting for round {self._CurMatch+1}.{self._CurGame+1} ({self._GetRound()+1}) to finish!")



    def _FinishGames(self):
        """
        Finish (sub)round, do post-processing.
        """
        self.tprint(f"Finishing round {self._CurMatch+1}.{self._CurGame+1} ({self._GetRound()+1})...")

        # Fetch user scores from Swiss event
        GameScores = dict()
        RequestEndpoint = f"https://lichess.org/api/swiss/{self._SwissId}/results"
        Response = self._RunGetRequest(RequestEndpoint, True, True)
        Lines = Response.iter_lines()
        for Line in Lines:
            JUser = json.loads(Line.decode("utf-8"))
            UserName = JUser["username"].lower()
            if UserName not in self._Participants:
                continue
            GameScores[UserName] = JUser["points"] - self._Participants[UserName]["points"]
            self._Participants[UserName]["points"] = JUser["points"]
        GameScores["BYE"] = 0

        # time.sleep(self._ApiDelay)

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

        self.tprint(f"Round {self._CurMatch+1}.{self._CurGame+1} ({self._GetRound()+1}) finished!")



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
                    if self._TopGetsWhite[self._CurMatch]:
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



    # =======================================================
    #       The main routine
    # =======================================================

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
