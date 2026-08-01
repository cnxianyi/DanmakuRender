@{
    # The default is 'python'. An absolute python.exe path is safer for Task Scheduler.
    PythonCommand = 'python'

    # Copy the Bark URL ending with your device key. Keep this file private.
    BarkUrl = 'https://api.day.app/your-device-key'

    PythonArguments = @('main.py', '--skip_update')
    NotificationCooldownSeconds = 300

    # Optional: customize which console lines count as bugs.
    ErrorPattern = '\[(ERROR|CRITICAL)\]|Traceback \(most recent call last\):|Unhandled exception|Fatal Python error'
}
