"""Cliente PostgreSQL mínimo, em Python puro (protocolo v3), suficiente para a plataforma.

Por que existe: a plataforma precisa falar com o PostgreSQL (Neon) sem depender de bibliotecas compiladas, e
este é exatamente o código exercitado pelos testes automatizados contra um PostgreSQL real.

Recursos: TLS (com SNI, exigido pelo Neon), autenticação SCRAM-SHA-256 / MD5 / senha em claro (só sob TLS),
consultas parametrizadas pelo protocolo estendido com instrução sem nome (compatível com pooler em modo transação),
envio em lote (pipeline) de instruções cujo resultado não é lido, e conversão dos tipos usados pelo sistema.
"""
import base64
import hashlib
import hmac
import os
import socket
import ssl
import struct
from urllib.parse import parse_qs, unquote, urlparse


class Error(Exception):
    def __init__(self, message, sqlstate=None, detail=None):
        super().__init__(message)
        self.sqlstate = sqlstate or ""
        self.detail = detail
        self.sql = None


class IntegrityError(Error):
    pass


class OperationalError(Error):
    """Falha de conexão/rede: a conexão não é mais utilizável."""


class Result:
    __slots__ = ("columns", "oids", "rows", "rowcount", "done", "sql")

    def __init__(self, sql=None):
        self.columns, self.oids, self.rows, self.rowcount, self.done, self.sql = [], [], [], -1, False, sql


_INT = {20, 21, 23, 26}
_FLOAT = {700, 701}


def _conv(oid, raw):
    if raw is None:
        return None
    if oid == 25 or oid == 1043:
        return raw.decode("utf-8")
    if oid in _INT:
        return int(raw)
    if oid == 16:
        return raw == b"t"
    if oid in _FLOAT:
        return float(raw)
    if oid == 1700:
        s = raw.decode()
        return float(s) if ("." in s or "e" in s.lower() or s == "NaN") else int(s)
    if oid == 17:
        return bytes.fromhex(raw[2:].decode()) if raw[:2] == b"\\x" else bytes(raw)
    return raw.decode("utf-8")


def _param(v):
    if v is None:
        return None
    if v is True:
        return b"1"
    if v is False:
        return b"0"
    if isinstance(v, (bytes, bytearray, memoryview)):
        return b"\\x" + bytes(v).hex().encode()
    if isinstance(v, str):
        return v.replace("\x00", "").encode("utf-8")
    return str(v).encode("utf-8")


def parse_dsn(dsn):
    u = urlparse(dsn)
    if u.scheme not in ("postgres", "postgresql"):
        raise Error("Endereço do banco inválido: use postgresql://usuario:senha@host/banco")
    q = {k: v[-1] for k, v in parse_qs(u.query).items()}
    return {"host": u.hostname or "localhost", "port": u.port or 5432, "user": unquote(u.username or ""),
            "password": unquote(u.password or ""), "database": unquote((u.path or "/").lstrip("/")) or unquote(u.username or ""),
            "sslmode": q.get("sslmode", "prefer")}


class Connection:
    def __init__(self, dsn, timeout=30):
        self.info = parse_dsn(dsn)
        self.timeout = timeout
        self.sock = None
        self.buf = b""
        self.pending = []      # mensagens de instruções em lote ainda não enviadas
        self.pending_res = []  # resultados (ainda vazios) das instruções em lote
        self.tx_status = b"I"
        self.server_version = ""
        self.secure = False
        self._connect()

    # ---------------------------------------------------------------- conexão
    def _connect(self):
        i = self.info
        try:
            s = socket.create_connection((i["host"], i["port"]), timeout=self.timeout)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if i["sslmode"] != "disable":
                s.sendall(struct.pack("!II", 8, 80877103))
                ans = s.recv(1)
                if ans == b"S":
                    ctx = ssl.create_default_context()
                    if os.environ.get("HORIZON_PG_INSECURE_TLS") == "1":
                        ctx.check_hostname = False
                        ctx.verify_mode = ssl.CERT_NONE
                    s = ctx.wrap_socket(s, server_hostname=i["host"])
                    self.secure = True
                elif i["sslmode"] in ("require", "verify-ca", "verify-full"):
                    raise OperationalError("O servidor de banco não aceitou conexão segura (TLS).")
            self.sock = s
        except (OSError, ssl.SSLError) as e:
            raise OperationalError(f"Não foi possível conectar ao banco de dados: {e}") from e
        params = b"".join(k.encode() + b"\0" + v.encode() + b"\0" for k, v in
                          (("user", i["user"]), ("database", i["database"]), ("client_encoding", "UTF8")))
        body = struct.pack("!I", 196608) + params + b"\0"
        self._raw(struct.pack("!I", len(body) + 4) + body)
        self._auth()

    def _auth(self):
        i = self.info
        scram = None
        while True:
            t, d = self._msg()
            if t == b"R":
                code = struct.unpack("!I", d[:4])[0]
                if code == 0:
                    continue
                if code == 3:
                    if not self.secure:
                        raise OperationalError("O servidor pediu senha em texto puro sem conexão segura; recusado.")
                    self._send(b"p", i["password"].encode() + b"\0")
                elif code == 5:
                    inner = hashlib.md5(i["password"].encode() + i["user"].encode()).hexdigest().encode()
                    self._send(b"p", b"md5" + hashlib.md5(inner + d[4:8]).hexdigest().encode() + b"\0")
                elif code == 10:
                    mechs = d[4:].split(b"\0")
                    if b"SCRAM-SHA-256" not in mechs:
                        raise OperationalError("Método de autenticação do banco não suportado.")
                    nonce = base64.b64encode(os.urandom(18)).decode()
                    bare = f"n=,r={nonce}"
                    first = ("n,," + bare).encode()
                    self._send(b"p", b"SCRAM-SHA-256\0" + struct.pack("!I", len(first)) + first)
                    scram = {"nonce": nonce, "bare": bare}
                elif code == 11:
                    sf = d[4:].decode()
                    f = dict(x.split("=", 1) for x in sf.split(","))
                    if not f["r"].startswith(scram["nonce"]):
                        raise OperationalError("Resposta de autenticação inválida do servidor.")
                    salted = hashlib.pbkdf2_hmac("sha256", i["password"].encode(), base64.b64decode(f["s"]), int(f["i"]))
                    ckey = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
                    final_wo = f"c=biws,r={f['r']}"
                    auth_msg = f"{scram['bare']},{sf},{final_wo}".encode()
                    sig = hmac.new(hashlib.sha256(ckey).digest(), auth_msg, hashlib.sha256).digest()
                    proof = base64.b64encode(bytes(a ^ b for a, b in zip(ckey, sig))).decode()
                    skey = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
                    scram["server_sig"] = base64.b64encode(hmac.new(skey, auth_msg, hashlib.sha256).digest()).decode()
                    self._send(b"p", f"{final_wo},p={proof}".encode())
                elif code == 12:
                    f = dict(x.split("=", 1) for x in d[4:].decode().split(","))
                    if f.get("v") != scram["server_sig"]:
                        raise OperationalError("O servidor de banco não comprovou conhecer a senha (SCRAM).")
                else:
                    raise OperationalError(f"Método de autenticação do banco não suportado (código {code}).")
            elif t == b"S":
                parts = d.split(b"\0")
                if parts[0] == b"server_version" and len(parts) > 1:
                    self.server_version = parts[1].decode()
            elif t == b"E":
                raise OperationalError("Banco de dados recusou a conexão: " + self._err(d)[0])
            elif t == b"Z":
                self.tx_status = d[:1]
                return
            # K (chave de cancelamento), N (avisos): ignorados

    # ---------------------------------------------------------------- baixo nível
    def _raw(self, data):
        try:
            self.sock.sendall(data)
        except (OSError, ssl.SSLError) as e:
            self.close()
            raise OperationalError(f"Conexão com o banco perdida: {e}") from e

    def _send(self, t, body):
        self._raw(t + struct.pack("!I", len(body) + 4) + body)

    def _read(self, n):
        while len(self.buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except (OSError, ssl.SSLError) as e:
                self.close()
                raise OperationalError(f"Conexão com o banco perdida: {e}") from e
            if not chunk:
                self.close()
                raise OperationalError("O banco de dados encerrou a conexão.")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _msg(self):
        head = self._read(5)
        return head[:1], self._read(struct.unpack("!I", head[1:])[0] - 4)

    @staticmethod
    def _err(d):
        f = {}
        for part in d.split(b"\0"):
            if part:
                f[chr(part[0])] = part[1:].decode("utf-8", "replace")
        return f.get("M", "erro"), f.get("C", ""), f.get("D")

    @staticmethod
    def _pack(t, body):
        return t + struct.pack("!I", len(body) + 4) + body

    def _stmt(self, sql, params):
        ps = [_param(p) for p in params]
        bind = b"\0\0" + struct.pack("!H", 0) + struct.pack("!H", len(ps))
        for p in ps:
            bind += struct.pack("!i", -1) if p is None else struct.pack("!I", len(p)) + p
        bind += struct.pack("!H", 0)
        return (self._pack(b"P", b"\0" + sql.encode("utf-8") + b"\0" + struct.pack("!H", 0)) + self._pack(b"B", bind) +
                self._pack(b"D", b"P\0") + self._pack(b"E", b"\0" + struct.pack("!I", 0)))

    # ---------------------------------------------------------------- API
    def defer(self, sql, params=()):
        """Enfileira uma instrução cujo resultado não será lido; vai junto com a próxima ida ao servidor."""
        self.pending.append(self._stmt(sql, params))
        r = Result(sql)
        self.pending_res.append(r)
        return r

    def flush(self):
        if self.pending:
            self._roundtrip()

    def execute(self, sql, params=()):
        r = self.defer(sql, params)
        self._roundtrip()
        return r

    def _roundtrip(self):
        targets = self.pending_res
        data = b"".join(self.pending) + self._pack(b"S", b"")
        self.pending, self.pending_res = [], []
        self._raw(data)
        idx, err = 0, None
        cur = targets[0]
        while True:
            t, d = self._msg()
            if t == b"T":
                cnt = struct.unpack("!H", d[:2])[0]
                pos = 2
                for _ in range(cnt):
                    end = d.index(b"\0", pos)
                    cur.columns.append(d[pos:end].decode())
                    cur.oids.append(struct.unpack("!I", d[end + 7:end + 11])[0])
                    pos = end + 19
            elif t == b"D":
                cnt = struct.unpack("!H", d[:2])[0]
                pos, row = 2, []
                for k in range(cnt):
                    ln = struct.unpack("!i", d[pos:pos + 4])[0]
                    pos += 4
                    if ln < 0:
                        row.append(None)
                    else:
                        row.append(_conv(cur.oids[k], d[pos:pos + ln]))
                        pos += ln
                cur.rows.append(row)
            elif t == b"C":
                tag = d.rstrip(b"\0").split(b" ")
                try:
                    cur.rowcount = int(tag[-1])
                except ValueError:
                    cur.rowcount = -1
                cur.done = True
                idx += 1
                cur = targets[idx] if idx < len(targets) else Result()
            elif t == b"I":   # consulta vazia
                cur.done = True
                idx += 1
                cur = targets[idx] if idx < len(targets) else Result()
            elif t == b"E":
                if err is None:
                    m, code, detail = self._err(d)
                    err = (IntegrityError if code.startswith("23") else Error)(m, code, detail)
                    err.sql = cur.sql   # instrução que falhou (as seguintes do lote são descartadas pelo servidor)
            elif t == b"Z":
                self.tx_status = d[:1]
                break
            # 1 (Parse ok), 2 (Bind ok), n (NoData), N (aviso), S (parâmetro): ignorados
        if err:
            raise err
        if idx != len(targets):
            raise Error("Resposta inesperada do banco de dados.")

    def script(self, sql):
        """Executa várias instruções de uma vez (protocolo simples). Usado em BEGIN/COMMIT e migrações."""
        self.flush()
        self._send(b"Q", sql.encode("utf-8") + b"\0")
        err = None
        while True:
            t, d = self._msg()
            if t == b"E" and err is None:
                m, code, detail = self._err(d)
                err = (IntegrityError if code.startswith("23") else Error)(m, code, detail)
            elif t == b"Z":
                self.tx_status = d[:1]
                break
        if err:
            raise err

    @property
    def closed(self):
        return self.sock is None

    def close(self):
        s, self.sock = self.sock, None
        self.pending, self.pending_res = [], []
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
