"""Offline robustness test for the relay: silent vendor + mock DTU."""
import json, os, socket, subprocess, sys, threading, time, urllib.request

BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def mock_vendor():
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 15999)); srv.listen(5)
    while True:
        try:
            c, _ = srv.accept()
        except OSError:
            return
        # keep the connection open but never send anything (silent cloud)
        threading.Thread(target=lambda cc: _drain(cc), args=(c,), daemon=True).start()

def _drain(sock):
    try:
        while True:
            if not sock.recv(4096):
                return
    except OSError:
        return

def mock_dtu(stop):
    c = socket.create_connection(("127.0.0.1", 15000), timeout=5)
    c.sendall(b"000000000000")          # DTU heartbeat
    buf = b""
    c.settimeout(0.5)
    while not stop.is_set():
        try:
            data = c.recv(4096)
        except socket.timeout:
            try: c.sendall(b"000000000000")
            except OSError: break
            continue
        except OSError:
            break
        if not data:
            break
        buf += data
        while len(buf) >= 6:
            length = (buf[4] << 8) | buf[5]
            if len(buf) < 6 + length:
                break
            frame, buf = buf[:6+length], buf[6+length:]
            tid, pdu = frame[:2], frame[6:]
            fn = pdu[1]
            if fn == 3:
                count = (pdu[4] << 8) | pdu[5]
                payload = b"".join((1000+i).to_bytes(2,"big") for i in range(count))
                body = bytes([1,3,len(payload)]) + payload
            elif fn == 6:
                body = pdu[:6]
            else:
                body = bytes([1, 0x83, 2])
            c.sendall(tid + b"\x00\x00" + len(body).to_bytes(2,"big") + body)
    c.close()

def main():
    vendor = threading.Thread(target=mock_vendor, daemon=True); vendor.start()
    env = dict(os.environ, VENDOR_IP="127.0.0.1", VENDOR_PORT="15999",
               LISTEN_PORT="15000", HTTP_PORT="18090", DNS_PORT="0",
               CLOUD_IDLE_TIMEOUT="3", DTU_IDLE_TIMEOUT="2", LOG_LEVEL="DEBUG")
    proc = subprocess.Popen([sys.executable, "-m", "bridge.main"], cwd=BRIDGE_DIR,
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(2)
    stop = threading.Event()
    dtu = threading.Thread(target=mock_dtu, args=(stop,), daemon=True); dtu.start()
    time.sleep(1.2)

    def health():
        with urllib.request.urlopen("http://127.0.0.1:18090/health", timeout=10) as r:
            return json.loads(r.read())

    # 1) injection works
    try:
        with urllib.request.urlopen("http://127.0.0.1:18090/api/read?addr=0x0064&count=2", timeout=10) as r:
            print("read:", json.loads(r.read()))
    except Exception as e:
        print("read FAILED:", e)

    # 2) silent vendor -> watchdog should reconnect
    time.sleep(6)
    h = health()
    print("watchdog_drops:", h.get("watchdog_drops"), "| reconnects:", h.get("reconnects"),
          "| reason:", h.get("last_drop_reason"))
    print("counters:", {k: h.get(k) for k in ("cloud_frames","dtu_frames","dtu_heartbeats","desync_bytes","queued_bytes")})
    ok = (h.get("watchdog_drops") or 0) >= 1 and "cloud idle" in (h.get("last_drop_reason") or "")
    print("WATCHDOG TEST:", "PASS" if ok else "FAIL")

    stop.set()
    proc.terminate()
    try: out = proc.communicate(timeout=5)[0]
    except Exception: out = ""
    print("\n--- bridge log (tail) ---")
    print("\n".join((out or "").splitlines()[-25:]))
    return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
