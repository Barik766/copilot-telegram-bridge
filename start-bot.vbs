' Hidden auto-start + keep-alive launcher for the Telegram bot.
' Registered as a "run at logon" scheduled task; runs with no window and
' restarts the bot a few seconds after it ever exits or crashes.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
batch = """" & scriptDir & "\run-bot.cmd" & """"
Do
    sh.Run batch, 0, True   ' 0 = hidden window, True = wait until it exits
    WScript.Sleep 5000
Loop
