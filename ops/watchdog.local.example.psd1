@{
    # The default is 'python'. An absolute python.exe path is safer for Task Scheduler.
    PythonCommand = 'python'

    # Keep the Telegram bot token and chat ID private.
    TelegramBotToken = '123456:ABCDEF'
    TelegramChatId = '7129142702'
    TelegramApiBase = 'https://api.telegram.org'
    # Optional: http://127.0.0.1:7890 or socks5://127.0.0.1:1080
    TelegramProxy = ''
    TelegramMessageThreadId = ''

    PythonArguments = @('main.py', '--skip_update')
    NotificationCooldownSeconds = 300

    # Optional: customize which console lines count as bugs.
    ErrorPattern = '\[(ERROR|CRITICAL)\]|Traceback \(most recent call last\):|Unhandled exception|Fatal Python error'
}
