# 模块职责：提供 MySQL 连接池和连接配置的兼容函数入口。

try:
    from .repositories import MySQLConnectionPool, _mysql_config
except ImportError:
    from repositories import MySQLConnectionPool, _mysql_config


def mysql_config() -> dict:
    return _mysql_config()


def get_mysql_connection():
    return MySQLConnectionPool().acquire()
