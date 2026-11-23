-- 玉衡5.0.app：在后台启动控制台（已经开着就只打开窗口），然后自己马上退出
on run
	set appPath to POSIX path of (path to me)
	do shell script "/bin/zsh " & quoted form of (appPath & "Contents/Resources/start.sh") & " > /dev/null 2>&1 &"
end run
