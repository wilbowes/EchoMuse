"""A Radar wizard upload must build a Radar image with the right init and
stamp emos.board=radar and emos.system on the cmdline."""
import asyncio
import json

import pytest
import em_emos_build as eb
import em_emos_ota
import em_routes_provision


@pytest.mark.parametrize('missing', [False, True])
def test_uploaded_radar_bundle_includes_wifi_and_stamps_board(missing, monkeypatch):
    monkeypatch.setattr(eb, "radar_initramfs_kernel", lambda kernel: kernel)
    import struct
    from test_emos_build import make_reference, fake_init
    zimage = bytearray(1024)
    struct.pack_into('<I', zimage, 0x24, 0x016f2818)
    files = {'init32-radar': fake_init(machine=40, elf_class=1)}
    if not missing:
        files.update({n: b'test tool' for n in em_emos_ota.SBIN_ASSETS})
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
    response = asyncio.run(
        em_routes_provision._post_provision_emos_image.__wrapped__(Request()))
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
        for name in em_emos_ota.SBIN_ASSETS:
            assert ('sbin/' + name).encode() in cpio
