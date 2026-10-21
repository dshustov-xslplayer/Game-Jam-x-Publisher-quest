Xsolla Upload (Game Jam Launch Quest edition)

macOS:   double-click "Xsolla Upload.command"
         If macOS says it "could not verify" the file (the app is not notarized by Apple):
           a) System Settings > Privacy & Security > scroll down > "Open Anyway" next to the file, or
           b) in Terminal run:  xattr -dr com.apple.quarantine ~/Downloads/xsolla-upload
              then double-click again.
Windows: double-click "Xsolla Upload.bat"

Needs Python 3.8+ (macOS offers to install it on first run; Windows: python.org).
Before you start: put Build Loader into Downloads (no need to unzip it): either the public archive
https://cdn.xsolla.net/launcher/build/Partners/build_loader.zip (holds the Mac and Windows tools)
or BuildLoader.zip from Publisher Account (Launcher > Edit launcher > Games > Set up > Builds > Game builds).
Then copy the API key of your game from the same page.

When the upload finishes the app prints a receipt (also copied to the clipboard and saved on the
Desktop as xsolla-quest-receipt.txt). Paste it into the quest page. It contains only a masked key.
The upload goes to the Draft stage: only testers see it. Nothing is published to players.
Based on the xsolla-upload tool (MIT). Not an official Xsolla product.
