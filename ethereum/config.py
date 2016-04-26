from ethereum import utils
import db
from db import BaseDB

default_config = dict(
    GENESIS_PREVHASH=b'\x00' * 32,
    GENESIS_COINBASE=b'\x00' * 20,
    GENESIS_NONCE=utils.zpad(utils.encode_int(42), 8),
    GENESIS_MIXHASH=b'\x00' * 32,
    GENESIS_TIMESTAMP=0,
    GENESIS_EXTRA_DATA=b'',
    GENESIS_INITIAL_ALLOC={},
    ACCOUNT_INITIAL_NONCE=0,
    MAX_EXTRADATA_LENGTH=32,
    HOMESTEAD_FORK_BLKNUM=1150000
)


class Env(object):

    def __init__(self, db, config=None, global_config=None):
        assert isinstance(db, BaseDB)
        self.db = db
        self.config = config or dict(default_config)
        self.global_config = global_config or dict()
