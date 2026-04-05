"""
Centralized logging configuration for Website LLM Analyzer.

Provides colored console output and rotating file logging with module-specific loggers.

Author: Refactored for proper logging
Created: 2026-02-10
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional


# ANSI color codes for terminal output (no external dependencies)
class Colors:
    """ANSI escape codes for colored terminal output."""
    RESET = '\033[0m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'


class ColoredFormatter(logging.Formatter):
    """Custom formatter that adds colors to log levels in console output."""
    
    LEVEL_COLORS = {
        logging.DEBUG: Colors.CYAN,
        logging.INFO: Colors.GREEN,
        logging.WARNING: Colors.YELLOW,
        logging.ERROR: Colors.RED,
        logging.CRITICAL: Colors.RED + Colors.BOLD,
    }
    
    def format(self, record):
        """Format log record with color for the level."""
        # Add color to the levelname
        levelname = record.levelname
        if record.levelno in self.LEVEL_COLORS:
            colored_levelname = f"{self.LEVEL_COLORS[record.levelno]}{levelname}{Colors.RESET}"
            record.levelname = colored_levelname
        
        # Format the message
        result = super().format(record)
        
        # Restore original levelname (for file handler)
        record.levelname = levelname
        
        return result


def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> None:
    """
    Configure root logger with console and file handlers.
    
    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Log file path (default: website_llm_analyzer.log in current dir)
    """
    if log_file is None:
        log_file = "website_llm_analyzer.log"
    
    # Convert string level to logging constant
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    
    # Get root logger and set level
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    
    # Clear any existing handlers
    root_logger.handlers.clear()
    
    # Format string for both handlers
    log_format = '[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s'
    date_format = '%Y-%m-%d %H:%M:%S'
    
    # Console handler with colors
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_formatter = ColoredFormatter(log_format, datefmt=date_format)
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)
    
    # File handler with rotation (5MB max, 3 backups)
    try:
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,  # 5MB
            backupCount=3,
            encoding='utf-8'
        )
        file_handler.setLevel(numeric_level)
        file_formatter = logging.Formatter(log_format, datefmt=date_format)
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)
    except Exception as e:
        # If file handler fails, log to console only
        root_logger.error(f"Failed to create file handler for {log_file}: {e}")


def get_logger(name: str) -> logging.Logger:
    """
    Get a module-specific logger.
    
    Args:
        name: Logger name (typically __name__ of the calling module)
    
    Returns:
        Configured logger instance
    """
    return logging.getLogger(name)


# Convenience function for tqdm-compatible logging
def get_tqdm_write_logger(logger: logging.Logger):
    """
    Return a write function compatible with tqdm.write().
    
    This allows logging to work seamlessly with tqdm progress bars.
    
    Args:
        logger: Logger instance
        
    Returns:
        Function that can be used as: tqdm.write(msg, file=write_func)
    
    Example:
        logger = get_logger(__name__)
        for item in tqdm(items):
            tqdm.write(f"Processing {item}", file=sys.stderr)
            logger.info(f"Processed {item}")
    """
    class TqdmLoggingHandler:
        def write(self, msg):
            if msg.strip():  # Avoid empty messages
                logger.info(msg.strip())
        
        def flush(self):
            pass
    
    return TqdmLoggingHandler()


# Initialize logging when module is imported (can be reconfigured later)
if not logging.getLogger().handlers:
    setup_logging()
