"""A Biscuit ARM payload must never be used for a Radar installation."""
import json

import pytest
import em_api
import em_emos_build as eb


@pytest.mark.asyncio
async def test_radar_refuses_release_with_only_biscuit_inits(monkeypatch):
    bundle = eb.build_payload_bundle({'init': b'aarch64', 'init32': b'biscuit'}, 'test')
    async def release():
        return {'version': 'test', 'assets': {em_api.EMOS_PAYLOAD_ASSET: {'url': 'test'}}}
    async def binary(*args):
        return bundle
    monkeypatch.setattr(em_api, '_fetch_latest_emos_release', release)
    monkeypatch.setattr(em_api, '_fetch_binary', binary)
    init, sbin, version, error = await em_api._fetch_emos_payload('arm', 'radar')
    assert init is None and sbin == {}
    assert error.status == 404
    assert 'init32-radar' in error.text


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', [False, True])
async def test_uploaded_radar_bundle_includes_wifi_and_stamps_board(missing, monkeypatch):
    monkeypatch.setattr(eb, "radar_initramfs_kernel", lambda kernel: kernel)
    import struct
    from test_emos_build import make_reference, fake_init
    zimage = bytearray(1024)
    struct.pack_into('<I', zimage, 0x24, 0x016f2818)
    files = {'init32-radar': fake_init(machine=40, elf_class=1)}
    if not missing:
        files.update({n: b'test tool' for n in em_api.EMOS_SBIN_ASSETS})
    fields = {'reference': make_reference(zimage=bytes(zimage)),
              'payload': eb.build_payload_bundle(files, 'test-radar'),
              'board': b'radar', 'system_part': b'13'}
    class Field:
        def __init__(self, name, data): self.name, self.data = name, data
        async def read(self): return self.data
    class Reader:
        def __init__(self): self.fields = iter(fields.items())
        async def next(self):
            pair = next(self.fields, None)
            return Field(*pair) if pair else None
    class Request:
        async def multipart(self): return Reader()
    response = await em_api._post_provision_emos_image.__wrapped__(Request())
    if missing:
        assert response.status == 400
        assert 'Wi-Fi tools' in response.text
    else:
        assert response.status == 200, response.text
        info = json.loads(response.headers['X-Build-Info'])
        assert info['board_id'] == 'radar'
        assert 'emos.board=radar' in info['cmdline']
        assert 'emos.system=/dev/block/mmcblk0p13' in info['cmdline']
        import gzip
        cpio = gzip.decompress(eb.split_reference(response.body)['ramdisk'])
        for name in em_api.EMOS_SBIN_ASSETS:
            assert ('sbin/' + name).encode() in cpio
