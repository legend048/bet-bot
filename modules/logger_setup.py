import logging
import colorlog
from pathlib import Path

def setup_logger(log_file: str = "./logs/bot.log") -> logging.Logger:
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("BharatBot")
    logger.setLevel(logging.DEBUG)
    terminal_handler = colorlog.StreamHandler()
    terminal_handler.setLevel(logging.INFO)
    terminal_handler.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s[%(levelname)s]%(reset)s %(cyan)s%(asctime)s%(reset)s → %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG":    "white",
            "INFO":     "green",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold_red",
        }
    ))
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "[%(levelname)s] %(asctime)s → %(funcName)s:%(lineno)d → %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(terminal_handler)
    logger.addHandler(file_handler)
    return logger
