-- dd.app — the native launcher. Compiled by install_app.sh with osacompile (built into macOS), so macOS runs dd
-- natively (not under Rosetta) and asks Microphone / Automation permission for "dd" itself.
-- It just runs Contents/Resources/launcher.sh and stays alive while dd runs (no Dock icon: LSUIElement).
on run
	set launcher to POSIX path of (path to me) & "Contents/Resources/launcher.sh"
	with timeout of 3153600000 seconds
		do shell script "/bin/bash " & quoted form of launcher & " >/dev/null 2>&1; true"
	end timeout
end run
