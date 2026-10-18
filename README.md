CFB Computer Poll
Version: 1.1.2
CFB Computer Poll is a college football ranking application that
calculates opponent-adjusted ratings for FBS and FCS teams using game
data from CollegeFootballData.com.
This README explains how to build the Windows executable, configure the
application, download season data, train the model, and view the
results.
Requirements
- A Windows computer
- The project source files
- A free API key from
  CollegeFootballData.com
1. Build the application
1. Open the project folder in File Explorer.
2. Locate and double-click build_exe.bat.
3. Allow the build process to finish. The first build may take a little
   while.
4. When the process completes successfully, open the dist folder.
5. Run the CFB Computer Poll executable located in that folder.
If the build reports an error, review the terminal output before closing
the window. The executable may not have been created if the build did
not complete successfully.
2. Add your API key
1. Open CFB Computer Poll.
2. Go to Setup & Data.
3. Get a free API key from
   CollegeFootballData.com if you
   do not already have one.
4. Paste the key into the API key field.
5. Click Save key.
Keep your API key private. Do not share it publicly or commit it to a
public source-code repository.
3. Download historical season data
Historical game data is used to give the model past results to work
with.
1. In Setup & Data, choose the starting and ending years under
   Historic seasons.
2. Click Download missing seasons.
3. Wait for the download to finish before starting another data
   operation.
4. Check the stored-season list to confirm which seasons and games are
   available.
The application may make roughly 60 API calls per downloaded season.
Check your API usage limits before downloading several seasons. Seasons
already stored locally generally do not need to be downloaded again
unless you intentionally refresh the data.
4. Update the current season
1. Set Current season to the season you want to work with.
2. Click Update current season.
3. Wait for the update to complete.
4. Confirm that the stored-season information reflects the available
   data.
The current-season update re-downloads the season data being updated. A
season in progress will naturally contain fewer games than a completed
season.
5. Train the model
After the required data has been downloaded:
1. Open the Train tab.
2. Follow the options and prompts shown in the application to configure
   and run training.
3. If the application offers historical or preseason warmup options,
   choose the appropriate option for your intended ranking run.
4. Allow training to finish before opening the rankings.
5. If the results look incomplete, verify that the necessary seasons
   and game data are stored, then run the appropriate training process
   again.
Training options can affect the resulting ratings. Use the same workflow
consistently when comparing polls across weeks or seasons.
6. View rankings and compare teams
- Rankings: Review the model's team rankings after training.
- Matchups: Use the matchup tools to compare teams, where
  available.
- Updates: Review or run available data/model update actions.
- Settings: Adjust the application's available preferences.
The rankings are the output of this computer model and are not official
AP, Coaches, or College Football Playoff rankings.
Data storage
The application stores its data and settings locally. The default
Windows location shown by the application is:
C:\Users\<YourUserName>\AppData\Roaming\CFBPoll
Replace <YourUserName> with your Windows account name. Because this
folder contains application data, avoid deleting it unless you intend to
remove the locally stored data and settings. Consider backing it up
before making major changes.
Troubleshooting
The executable does not appear in dist
- Run build_exe.bat again.
- Check the build output for errors.
- Confirm that the project dependencies are installed as required by
  the project.
The API key is rejected or data will not download
- Confirm that the key was copied correctly and saved.
- Check that your CollegeFootballData.com account/key is active.
- Check your API usage limits.
- Try again later if the service is temporarily unavailable.
Rankings are missing or look incorrect
- Confirm that the intended season data has been downloaded.
- Check that the current-season update completed.
- Run the appropriate training or warmup workflow.
- Make sure you are viewing the intended season and poll output.
A download or update appears stuck
- Give the operation time to complete, especially when downloading
  multiple seasons.
- Check the status text and progress bar.
- Use Cancel if necessary, then review the stored-season
  information before trying again.
Notes
- An internet connection is required for API-based data downloads and
  updates.
- API availability, rate limits, and data coverage are controlled by
  CollegeFootballData.com.
- Application behavior and tab names may change between versions.
