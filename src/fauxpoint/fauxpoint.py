from asyncio import subprocess
from datetime import datetime as DateTime, timedelta as TimeDelta, timezone as TimeZone
import ipaddress
import logging
import os
import platformdirs
import re
import secrets
import subprocess
import sys
import tempfile
from typing import Optional, Final, final
from unittest import result
from fastapi import FastAPI, Form, responses, Depends, Request, Response, HTTPException
import slowapi  # https://slowapi.readthedocs.io/en/latest/
from sqlmodel import Field, Session, SQLModel, create_engine, select, sql
import sqlalchemy

assert sys.version_info >= (3, 8)
sql.expression.Select.inherit_cache = False  # https://github.com/tiangolo/sqlmodel/issues/189
sql.expression.SelectOfScalar.inherit_cache = False

### command-line interface


def cli(return_help_text=False):
    import argparse  # https://docs.python.org/3/library/argparse.html

    help_width = 78 if return_help_text else None  # consistent width for README.py
    formatter_class = lambda prog: argparse.HelpFormatter(
        prog,
        max_help_position=33,
        width=help_width,
    )
    parser = argparse.ArgumentParser(
        prog=app_name(),
        formatter_class=formatter_class,
    )
    db_file_display = db_pathname().replace(os.path.expanduser('~'), '~')
    parser.add_argument(
        "--dbfile",
        type=str,
        default='',  # need to call db_pathname() again later with create_dir=True
        help=f"path for database file ('-' for memory-only; default: {db_file_display})",
    )
    parser.add_argument(
        "-l",
        "--logfile",
        type=str,
        default='-',
        help="path for log file (default: write to STDERR)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action='append_const',
        const=-1,
        dest="verbose",  # mapping:  "-q"->ERROR / ""->WARNING / "-v"->INFO / "-vv"->DEBUG
        help="silence warning messages",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action='append_const',
        const=1,
        help="increase verbosity",
    )
    if return_help_text:  # used by README.py
        return parser.format_help()
    args = parser.parse_args()
    args.log_level = 2 + (0 if args.verbose is None else sum(args.verbose))
    del args.verbose
    if args.log_level >= 3:  # info or debug
        log_format = '%(asctime)s.%(msecs)03d %(levelname)s %(message)s'
    else:
        log_format = '%(message)s'
    logging.basicConfig(
        format=log_format,
        datefmt='%H:%M:%S',
        filename=args.logfile if args.logfile != '-' else None,
        filemode='a',
    )
    logger = logging.getLogger(app_name())
    log_levels = [
        logging.CRITICAL,
        logging.ERROR,
        logging.WARNING,
        logging.INFO,
        logging.DEBUG,
        logging.DEBUG,  # corresponds to 'trace' in uvicorn
    ]
    try:
        logger.setLevel(log_levels[args.log_level])
    except IndexError:
        logger.setLevel(logging.WARNING)
        error = "Invalid log level"
        logger.error(error)
        raise ValueError(error)
    return args


### DB table 'dev' - WireGuard interfaces (often just a single interface)

wg_dev_map = list()  # map Dev.id to wgX WireGuard device
ipv4_map = list()  # map Dev.id to ipv4_base
ipv6_map = list()  # map Dev.id to ipv6_base
reserved_ips = 38


def app_name():
    return os.path.splitext(os.path.basename(__file__))[0]


class Dev(SQLModel, table=True):
    id: Optional[int] = Field(primary_key=True, default=None)
    comment: str
    ipv4_base: str
    ipv6_base: str
    privkey: str
    pubkey: str
    listening_port: int

    def __init__(self):
        self.comment = ""
        # IPv4 base is 10. + random xx.xx. + 0
        self.ipv4_base = str(ipaddress.ip_address('10.0.0.0') + secrets.randbelow(2**16) * 2**8)
        # IPv6 base is fdf9 (fauxpoint) + 2 random groups + 5 0000 groups
        self.ipv6_base = str(ipaddress.ip_address('fdf9::') + secrets.randbelow(2**32) * 2**80)
        self.privkey = sudo_wg(['genkey'])
        self.pubkey = sudo_wg(['pubkey'], input=self.privkey)
        self.listening_port = 123

    def iface(self):
        return f'wg{wg_dev_map[self.id]}'

    def ipv4(self):
        # ending in '/32' feels cleaner but client can't ping, even if client uses
        # `ip address add dev wg0 10.110.169.40 peer 10.110.169.1`
        # fix seems to be `ip -4 route add .../18 dev wg0` on server or use '/18' below
        return str(ipaddress.ip_address(self.ipv4_base) + 1) + '/18'  # max 16000 clients

    def ipv6(self):
        return str(ipaddress.ip_address(self.ipv6_base) + 1) + '/114'  # max 16000 clients

    @staticmethod
    def startup():
        logger = logging.getLogger(app_name())
        sudo_sysctl('net.ipv4.ip_forward=1')
        sudo_sysctl('net.ipv6.conf.all.forwarding=1')
        with Session(engine) as session:
            dev_count = session.query(Dev).count()
        if dev_count == 0:  # first run--need to define a WireGuard device
            with Session(engine) as session:
                dev = Dev()
                session.add(dev)
                session.commit()
        with Session(engine) as session:
            statement = select(Dev)
            results = session.exec(statement)
            assert len(wg_dev_map) == 0
            for i in results:  # initialize network for each WireGuard dev
                next_unused_dev = 0 if len(wg_dev_map) == 0 else wg_dev_map[-1] + 1
                while True:  # find next available wgX dev
                    wg_dev = f'wg{next_unused_dev}'
                    try:
                        sudo_ip(['address', 'show', 'dev', wg_dev])
                    except RuntimeError:  # assume error is: Device "wgX" does not exist.
                        break
                    next_unused_dev += 1
                wg_dev_map.append(next_unused_dev)
                ipv4_map.append(i.ipv4_base)
                ipv6_map.append(i.ipv6_base)
                assert len(wg_dev_map) == i.id  # wg_dev_map and Devs should be 1:1
                # configure wgX; see `systemctl status wg-quick@wg0.service`
                sudo_ip(['link', 'add', 'dev', wg_dev, 'type', 'wireguard'])
                sudo_ip(['link', 'set', 'mtu', '1420', 'up', 'dev', wg_dev])
                sudo_ip(['-4', 'address', 'add', 'dev', wg_dev, i.ipv4()])
                sudo_ip(['-6', 'address', 'add', 'dev', wg_dev, i.ipv6()])
                sudo_wg(['set', wg_dev, 'private-key', f'!FILE!{i.privkey}'])
                sudo_wg(['set', wg_dev, 'listen-port', str(i.listening_port)])
                sudo_iptables(
                    '--append FORWARD'.split(' ')
                    + f'--in-interface {wg_dev}'.split(' ')
                    + '--jump ACCEPT'.split(' ')
                )
                sudo_iptables(
                    '--table nat'.split(' ')
                    + '--append POSTROUTING'.split(' ')
                    + '--out-interface eth0'.split(' ')  # FIXME: not necessarily eth0
                    + '--jump MASQUERADE'.split(' ')
                )

    @staticmethod
    def shutdown():
        global wg_dev_map
        sudo_undo_iptables()
        for i in wg_dev_map:
            sudo_ip(['link', 'del', 'dev', f'wg{i}'])
        wg_dev_map = list()


### DB table 'user' - person managing VPN clients

base28_digits: Final[str] = '23456789BCDFGHJKLMNPQRSTVWXZ'  # avoid bad words, 1/i, 0/O


class User(SQLModel, table=True):
    __table_args__ = (sqlalchemy.UniqueConstraint('account'),)  # must have a unique account code
    id: Optional[int] = Field(primary_key=True, default=None)
    account: str = Field(  # e.g. 'L7V2BCMM3PRKVF2'
        index=True,
        default_factory=lambda: ''.join(secrets.choice(base28_digits) for i in range(15)),
    )
    clients_max: int = 7
    created_at: DateTime = Field(
        sa_column=sqlalchemy.Column(
            sqlalchemy.DateTime(timezone=True),
            default=DateTime.utcnow,
        )
    )
    valid_until: DateTime = Field(
        sa_column=sqlalchemy.Column(
            sqlalchemy.DateTime(timezone=True),
            default=lambda: DateTime.utcnow() + TimeDelta(days=3650),
        )
    )
    comment: str

    def formatted_account(self):  # display version, e.g. 'L7V.2BC.MM3.PRK.VF2'
        return '.'.join(self[i : i + 3] for i in range(0, 15, 3))

    @staticmethod
    def validate_account(a):
        if len(a) != 15:
            raise HTTPException(status_code=422, detail="Account length must be 15")
        if not set(base28_digits).issuperset(a):
            raise HTTPException(status_code=422, detail="Invalid account characters")
        with Session(engine) as session:
            statement = select(User).where(User.account == a)
            result = session.exec(statement).one_or_none()
        if result is None:
            raise HTTPException(status_code=422, detail="Account not found")
        if result.valid_until.replace(tzinfo=TimeZone.utc) < DateTime.now(TimeZone.utc):
            raise HTTPException(status_code=422, detail="Account expired")
        # FIXME: verify pubkey limit
        return result

    @staticmethod
    def startup():
        with Session(engine) as session:
            user_count = session.query(User).count()
        if user_count == 0:  # first run--need to define a master account
            with Session(engine) as session:
                user = User()
                user.clients_max = 0  # reserve master account for account creation, not VPNs
                user.comment = "master account"
                session.add(user)
                session.commit()


### DB table 'client' - VPN client device


class Client(SQLModel, table=True):
    __table_args__ = (sqlalchemy.UniqueConstraint('pubkey'),)  # no 2 clients may share a key
    id: Optional[int] = Field(primary_key=True, default=None)
    user_id: int = Field(index=True, foreign_key='user.id')
    pubkey: str
    dev_id: int = Field(foreign_key='dev.id')
    # preshared_key: str
    # keepalive: int

    def ip_list(self):  # calculate client's 2 IP addresses for allowed-ips
        ipv4 = ipaddress.ip_address(ipv4_map[self.dev_id]) + (reserved_ips + self.id)
        ipv6 = ipaddress.ip_address(ipv6_map[self.dev_id]) + (reserved_ips + self.id)
        return f'{ipv4}/32,{ipv6}/128'

    def set_peer(self):
        sudo_wg(  # see https://www.man7.org/linux/man-pages/man8/wg.8.html
            f'set {self.iface()}'.split(' ')
            + f'peer {self.pubkey}'.split(' ')
            # consider: + f'preshared-key !FILE!(self.preshared_key)}'  # see man page
            # consider: + f'persistent-keepalive {self.keepalive}'  # see man page
            + f'allowed-ips {self.ip_list()}'.split(' ')
        )

    def iface(self):
        return f'wg{wg_dev_map[self.dev_id]}'  # e.g.: 'wg0'

    @staticmethod
    def validate_pubkey(k):
        if not (42 <= len(k) < 72):
            raise HTTPException(status_code=422, detail="Invalid pubkey length")
        if re.search(r'[^A-Za-z0-9/+=]', k):
            raise HTTPException(status_code=422, detail="Invalid pubkey characters")

    @staticmethod
    def startup():
        with Session(engine) as session:
            statement = select(Client)
            results = session.exec(statement)
            for c in results:  # let wg know about each valid peer
                c.set_peer()


### helper methods


def sudo_sysctl(args):
    arg_list = args if type(args) is list else [args]
    return run_external(['sudo', 'sysctl'] + arg_list)


sudo_iptables_log = list()


def sudo_iptables(args):
    sudo_iptables_log.append(args)
    return run_external(['sudo', 'iptables'] + args)


def sudo_undo_iptables():
    global sudo_iptables_log
    for args in sudo_iptables_log:
        exec = ['sudo', 'iptables'] + args
        for i, a in enumerate(exec):  # invert '--append'
            if a == '--append' or a == '--insert' or a == '-A' or a == '-I':
                exec[i] = '--delete'
        run_external(exec)
    sudo_iptables_log = list()


def sudo_ip(args):
    return run_external(['sudo', 'ip'] + args)


def sudo_wg(args, input=None):
    exec = ['sudo', 'wg'] + args
    to_delete = list()
    for i, a in enumerate(exec):  # replace '!FILE!...' args with a temp file
        if a.startswith('!FILE!'):
            h = tempfile.NamedTemporaryFile(delete=False)
            h.write(a[6:].encode())
            h.close()
            to_delete.append(h.name)
            exec[i] = h.name
    try:
        r = run_external(exec, input=input)
    except Exception as e:
        raise e
    finally:
        for f in to_delete:  # remove temp file(s)
            os.unlink(f)
    return r


def run_external(args, input=None):
    logger = logging.getLogger(app_name())
    logger.info(f"running: {' '.join(args)}")
    exec_count = 2 if args[0] == 'sudo' else 1
    for i, a in enumerate(args[:exec_count]):  # e.g. expand 'wg' to '/usr/bin/wg'
        for p in '/usr/sbin:/usr/bin:/sbin:/bin'.split(':'):
            joined = os.path.join(p, a)
            if os.path.isfile(joined):
                args[i] = joined
                break
    proc = subprocess.run(
        args,
        input=None if input is None else input.encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"`{' '.join(args)}` returned error: {proc.stderr.decode().rstrip()}")
    return proc.stdout.decode().rstrip()


### startup and shutdown


def mkdir_r(path):  # like Linux `mkdir --parents`
    if path == '':
        return
    base = os.path.dirname(path)
    if not os.path.exists(base):
        mkdir_r(base)
    os.makedirs(path, exist_ok=True)
    # except (PermissionError, FileNotFoundError, NotADirectoryError):
    #     one of these will be raised if the directory cannot be created


def db_pathname(create_dir=False):
    config_dir = platformdirs.user_config_dir(app_name())
    if create_dir:
        mkdir_r(config_dir)
    return os.path.join(config_dir, f'data.sqlite')


engine = None
app = FastAPI()
limiter = slowapi.Limiter(key_func=slowapi.util.get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(slowapi.errors.RateLimitExceeded, slowapi._rate_limit_exceeded_handler)


@app.on_event('startup')
def on_startup():
    args = cli()  # https://www.uvicorn.org/deployment/#running-programmatically
    global engine
    if engine:  # initialize once
        return
    if args.dbfile == '':  # use default location
        db_file = db_pathname(create_dir=True)
    elif args.dbfile == '-':
        db_file = ':memory:'
    else:
        db_file = args.dbfile
    engine = create_engine(f'sqlite:///{db_file}', echo=(args.log_level >= 4))
    SQLModel.metadata.create_all(engine)
    try:
        Dev.startup()  # configure wgX
        User.startup()  # add master account on first run
        Client.startup()  # add peers to wgX
    except Exception as e:
        on_shutdown()
        raise e
    logger = logging.getLogger(app_name())
    logger.debug(f"initialization complete")


@app.on_event('shutdown')
def on_shutdown():
    Dev.shutdown()


### web API


@app.get('/pubkeys/{account}')
# @limiter.limit('10/minute')  # FIXME: uncomment this to make brute-forcing account harder
def get_pubkeys(account: str):
    user = User.validate_account(account)
    with Session(engine) as session:
        statement = select(Client).where(Client.user_id == user.id)
        results = session.exec(statement)
        return [c.pubkey for c in results]


@app.post('/wg/', response_class=responses.PlainTextResponse)
@limiter.limit('100/minute')  # FIXME: reduce to 10
def new_client(request: Request, account: str = Form(...), pubkey: str = Form(...)):
    user = User.validate_account(account)
    with Session(engine) as session:
        user_client_count = session.query(Client).filter(Client.user_id == user.id).count()
    if user_client_count >= user.clients_max:
        raise HTTPException(status_code=422, detail="No additional clients are allowed")
    Client.validate_pubkey(pubkey)
    with Session(engine) as session:  # look for pubkey in database
        statement = select(Client).where(Client.pubkey == pubkey)
        first = session.exec(statement).first()
    if first is not None:
        if first.user_id != user.id:  # different user already has this pubkey
            raise HTTPException(status_code=422, detail="Public key already in use")
        return first.ip_list()  # return existing IPs for this pubkey
    with Session(engine) as session:
        client = Client(
            user_id=user.id,
            pubkey=pubkey,
            dev_id=0,  # FIXME: figure out how to do multiple devs
        )
        session.add(client)
        session.commit()  # FIXME: possible race condition where user could exceed clients_max
        client.set_peer()  # configure WireGuard for this peer
        return client.ip_list()


@app.post('/new_account/', response_class=responses.PlainTextResponse)
@limiter.limit('100/minute')  # FIXME: reduce to 10
def new_account(request: Request, master_account: str = Form(...), comment: str = Form(...)):
    master_user = User.validate_account(master_account)
    if master_user.id != 1:
        raise HTTPException(status_code=422, detail="Master account code required")
    if len(comment) > 99:
        raise HTTPException(status_code=422, detail="Comment too long")
    with Session(engine) as session:
        account = User(comment=comment)
        session.add(account)
        session.commit()
        return account.account


@app.get('/raise_error')
def get_pubkeys():
    raise HTTPException(status_code=404, detail="Test exception from /raise_error")


def entry_point():  # called from setup.cfg
    import uvicorn  # https://www.uvicorn.org/

    try:
        uvicorn.run(  # https://www.uvicorn.org/deployment/#running-programmatically
            f'{app_name()}:app',
            host='',  # both IPv4 and IPv6; for one use '0.0.0.0' or '::0'
            port=8000,
            # workers=3,  # FIXME
            workers=1,
            log_level='debug'
            # FIXME: generate self-signed TLS cert based on IP address:
            #     mkdir -p ../.ssl/private ../.ssl/certs
            #     IP=$(echo $SSH_CONNECTION |grep -Po "^\S+\s+\S+\s+\K\S+")
            #     openssl req -new -x509 -nodes -days 3650 -newkey rsa:2048 -keyout ../.ssl/private/fastapiselfsigned.key -out ../.ssl/certs/fastapiselfsigned.crt -subj "/C=  /ST=  /L=   /O=   /OU=   /CN=$IP"
            # enable TLS in uvicorn.run():
            #     ssl_keyfile='../.ssl/private/fastapiselfsigned.key',
            #     ssl_certfile='../.ssl/certs/fastapiselfsigned.crt',
        )
    except KeyboardInterrupt:
        on_shutdown()
        sys.exit()
    except Exception as e:
        on_shutdown()
        raise e
