"""The device link negotiates the Echo's preferred cipher, not ours (em_pki)."""

import ssl

import em_pki


def _handshake(server_ctx, client_ctx):
    s_in, s_out, c_in, c_out = (ssl.MemoryBIO() for _ in range(4))
    server = server_ctx.wrap_bio(s_in, s_out, server_side=True)
    client = client_ctx.wrap_bio(c_in, c_out, server_hostname="echomuse-controller")
    for _ in range(20):
        done = 0
        for side, out, peer_in in ((client, c_out, s_in), (server, s_out, c_in)):
            try:
                side.do_handshake()
                done += 1
            except ssl.SSLWantReadError:
                pass
            data = out.read()
            if data:
                peer_in.write(data)
        if done == 2:
            return client.cipher()[0]
    raise AssertionError("handshake did not complete")


def _client(tls_dir, ciphers):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cadata=em_pki.ca_pem(tls_dir))
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2  # where Python can order suites
    ctx.set_ciphers(ciphers)
    return ctx


def test_an_echo_that_prefers_chacha20_gets_it(tmp_path):
    """An armv7 Echo (Go, no AES assembly) lists ChaCha20 first; the server
    preferring its own AES-256-GCM cost it 3.4% of a core per reply."""
    em_pki._generate(str(tmp_path))
    server = em_pki.server_ssl_context(str(tmp_path))
    assert not server.options & ssl.OP_CIPHER_SERVER_PREFERENCE
    got = _handshake(server, _client(
        tmp_path, "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:"
                  "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384"))
    assert "CHACHA20" in got, got


def test_an_echo_that_prefers_aes_gets_it(tmp_path):
    """And a client with AES hardware (arm64, x86) keeps AES: its order wins too."""
    em_pki._generate(str(tmp_path))
    got = _handshake(em_pki.server_ssl_context(str(tmp_path)), _client(
        tmp_path, "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
                  "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305"))
    assert "AES128" in got, got
