import sys
import rlp
from rlp.sedes import CountableList, binary
from rlp.utils import decode_hex, encode_hex, ascii_chr, str_to_bytes
from ethereum import opcodes
from ethereum import utils
from ethereum import specials
from ethereum import bloom
import vm 
from ethereum.exceptions import *
from ethereum.utils import safe_ord, normalize_address

sys.setrecursionlimit(100000)

from ethereum.slogging import get_logger
log_tx = get_logger('eth.pb.tx')
log_msg = get_logger('eth.pb.msg')
log_state = get_logger('eth.pb.msg.state')

TT255 = 2 ** 255
TT256 = 2 ** 256
TT256M1 = 2 ** 256 - 1

OUT_OF_GAS = -1

# contract creating transactions send to an empty address
CREATE_CONTRACT_ADDRESS = b''


def mk_contract_address(sender, nonce):
    return utils.sha3(rlp.encode([normalize_address(sender), nonce]))[12:]


def verify(block, parent):
    import blocks
    try:
        block2 = rlp.decode(rlp.encode(block), blocks.Block,
                            env=parent.env, parent=parent)
        assert block == block2
        return True
    except blocks.VerificationFailed:
        return False


class Log(rlp.Serializable):

    # TODO: original version used zpad (here replaced by int32.serialize); had
    # comment "why zpad"?
    fields = [
        ('address', utils.address),
        ('topics', CountableList(utils.int32)),
        ('data', binary)
    ]

    def __init__(self, address, topics, data):
        if len(address) == 40:
            address = decode_hex(address)
        assert len(address) == 20
        super(Log, self).__init__(address, topics, data)

    def bloomables(self):
        return [self.address] + [utils.int32.serialize(x) for x in self.topics]

    def to_dict(self):
        return {
            "bloom": encode_hex(bloom.b64(bloom.bloom_from_list(self.bloomables()))),
            "address": encode_hex(self.address),
            "data": b'0x' + encode_hex(self.data),
            "topics": [encode_hex(utils.int32.serialize(t))
                       for t in self.topics]
        }

    def __repr__(self):
        return '<Log(address=%r, topics=%r, data=%r)>' %  \
            (encode_hex(self.address), self.topics, self.data)



def validate_transaction(block, tx):

    def rp(what, actual, target):
        return '%r: %r actual:%r target:%r' % (tx, what, actual, target)

    # (1) The transaction signature is valid;
    if not tx.sender:  # sender is set and validated on Transaction initialization
        raise UnsignedTransaction(tx)

    # (2) the transaction nonce is valid (equivalent to the
    #     sender account's current nonce);
    acctnonce = block.get_nonce(tx.sender)
    if acctnonce != tx.nonce:
        raise InvalidNonce(rp('nonce', tx.nonce, acctnonce))

    return True


def apply_transaction(block, tx):
    validate_transaction(block, tx)

    log_tx.debug('TX NEW', tx_dict=tx.log_dict())
    # start transacting #################
    block.increment_nonce(tx.sender)
    # print block.get_nonce(tx.sender), '@@@'

    if block.number >= block.config['HOMESTEAD_FORK_BLKNUM']:
        assert tx.s * 2 < transactions.secpk1n
                
    # buy startgas
    message_data = vm.CallData([safe_ord(x) for x in tx.data], 0, len(tx.data))
    message = vm.Message(tx.sender, tx.to, tx.value, message_data, code_address=tx.to)

    # MESSAGE
    ext = VMExt(block, tx)
    if tx.to and tx.to != CREATE_CONTRACT_ADDRESS:
        result, data = apply_msg(ext, message)
        log_tx.debug('_res_', result=result, data=data)
    else:  # CREATE
        result, data = create_contract(ext, message)
        log_tx.debug('_create_', result=result, data=data)

    log_tx.debug("TX APPLIED", result=result, data=data)

    if not result:  # 0 = OOG failure in both cases
        log_tx.debug('TX FAILED')
        output = b''
        success = 0
    else:
        log_tx.debug('TX SUCCESS', data=data)
        # sell remaining gas
        if tx.to:
            output = b''.join(map(ascii_chr, data))
        else:
            output = data
        success = 1
    block.commit_state()
    suicides = block.suicides
    block.suicides = []
    for s in suicides:
        block.ether_delta -= block.get_balance(s)
        block.set_balance(s, 0)
        block.del_account(s)
    block.add_transaction_to_list(tx)
    block.logs = []
    return success, output


# External calls that can be made from inside the VM. To use the EVM with a
# different blockchain system, database, set parameters for testing, just
# swap out the functions here
class VMExt():

    def __init__(self, block, tx):
        self._block = block
        self.get_code = block.get_code
        self.get_balance = block.get_balance
        self.set_balance = block.set_balance
        self.set_storage_data = block.set_storage_data
        self.get_storage_data = block.get_storage_data
        self.log_storage = lambda x: block.account_to_dict(x)['storage']
        self.add_suicide = lambda x: block.suicides.append(x)
        self.block_hash = lambda x: block.get_ancestor_hash(block.number - x) \
            if (1 <= block.number - x <= 256 and x <= block.number) else b''
        self.block_coinbase = block.coinbase
        self.block_timestamp = block.timestamp
        self.block_number = block.number
        self.log = lambda addr, topics, data: \
            block.add_log(Log(addr, topics, data))
        self.tx_origin = tx.sender
        self.create = lambda msg: create_contract(self, msg)
        self.msg = lambda msg: _apply_msg(self, msg, self.get_code(msg.code_address))
        self.account_exists = block.account_exists


def apply_msg(ext, msg):
    return _apply_msg(ext, msg, ext.get_code(msg.code_address))


def _apply_msg(ext, msg, code):
    trace_msg = log_msg.is_active('trace')
    if trace_msg:
        log_msg.debug("MSG APPLY", sender=encode_hex(msg.sender), to=encode_hex(msg.to), value=msg.value,
                      data=encode_hex(msg.data.extract_all()))
        if log_state.is_active('trace'):
            log_state.trace('MSG PRE STATE SENDER', account=msg.sender,
                            bal=ext.get_balance(msg.sender),
                            state=ext.log_storage(msg.sender))
            log_state.trace('MSG PRE STATE RECIPIENT', account=msg.to,
                            bal=ext.get_balance(msg.to),
                            state=ext.log_storage(msg.to))
        # log_state.trace('CODE', code=code)
    # Transfer value, instaquit if not enough
    snapshot = ext._block.snapshot()
    if not ext._block.transfer_value(msg.sender, msg.to, msg.value):
        log_msg.debug('MSG TRANSFER FAILED', have=ext.get_balance(msg.to),
                      want=msg.value)
        return 1, []
    # Main loop
    if msg.code_address in specials.specials:
        res, dat = specials.specials[msg.code_address](ext, msg)
    else:
        res, dat = vm.vm_execute(ext, msg, code)
    # gas = int(gas)
    # assert utils.is_numeric(gas)
    if trace_msg:
        log_msg.debug('MSG APPLIED', sender=msg.sender, to=msg.to, data=dat)
        if log_state.is_active('trace'):
            log_state.trace('MSG PRE STATE SENDER', account=msg.sender,
                            bal=ext.get_balance(msg.sender),
                            state=ext.log_storage(msg.sender))
            log_state.trace('MSG PRE STATE RECIPIENT', account=msg.to,
                            bal=ext.get_balance(msg.to),
                            state=ext.log_storage(msg.to))

    if res == 0:
        log_msg.debug('REVERTING')
        ext._block.revert(snapshot)

    return res, dat


def create_contract(ext, msg):
    #print('CREATING WITH GAS', msg.gas)
    sender = decode_hex(msg.sender) if len(msg.sender) == 40 else msg.sender
    if ext.tx_origin != msg.sender:
        ext._block.increment_nonce(msg.sender)
    nonce = utils.encode_int(ext._block.get_nonce(msg.sender) - 1)
    msg.to = mk_contract_address(sender, nonce)
    b = ext.get_balance(msg.to)
    if b > 0:
        ext.set_balance(msg.to, b)
        ext._block.set_nonce(msg.to, 0)
        ext._block.set_code(msg.to, b'')
        ext._block.reset_storage(msg.to)
    msg.is_create = True
    # assert not ext.get_code(msg.to)
    code = msg.data.extract_all()
    msg.data = vm.CallData([], 0, 0)
    res, dat = _apply_msg(ext, msg, code)

    if res:
        if not len(dat):
            return 1, msg.to
        else:
            dat = []
            if ext._block.number >= ext._block.config['HOMESTEAD_FORK_BLKNUM']:
                return 0, b''
            log_msg.debug('CONTRACT CREATION OOG')
        ext._block.set_code(msg.to, b''.join(map(ascii_chr, dat)))
        return 1, msg.to
    else:
        return 0, b''
