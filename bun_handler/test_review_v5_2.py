"""Discoverable adapter for the supplied v5.2 review regressions.

The TestCase is retained verbatim. Only the standalone argparse/import setup is
replaced so unittest discovery cannot consume the parent process arguments.
The complete unchanged input is in review_material/.
"""
import struct
import unittest
import bun_handler as h
from test_bun_windows import build_bun_pe, rebuild_tweakcc_pe, append_pr_pe

class ReviewRegressions(unittest.TestCase):
    def pair(self,width,modern=False):
        kw=dict(cert=False,modern=modern,entry_name='B:/~BUN/root/cli',source=b'console.log("original");'+b' '*2048)
        current=build_bun_pe(section_header_size=width,**kw)
        reference=build_bun_pe(section_header_size=12-width,**kw)
        return current,reference
    def test_recovery_rejects_reference_wrapper_width_mismatch(self):
        for width in [4,8]:
            for modern in [False,True]:
                with self.subTest(width=width,modern=modern):
                    data,reference=self.pair(width,modern)
                    rebuilt=rebuild_tweakcc_pe(data,b'console.log("changed");')
                    with self.assertRaises(h.BunFormatError):h.recover_tweakcc(rebuilt,reference)
    def test_compaction_rejects_reference_wrapper_width_mismatch(self):
        for width in [4,8]:
            with self.subTest(width=width):
                data,reference=self.pair(width)
                appended=append_pr_pe(data,b'console.log("changed");')
                with self.assertRaises(h.BunFormatError):h.compact_with_reference(appended,reference)
    def test_valid_non_bun_pe_is_not_reported_as_malformed(self):
        data=bytearray(build_bun_pe(cert=False))
        peoff=struct.unpack_from('<I',data,60)[0];opt=peoff+24
        table=opt+struct.unpack_from('<H',data,peoff+20)[0]
        data[table+40:table+48]=b'.data\0\0\0'
        label=h.detect_format(bytes(data))
        self.assertNotIn('malformed',label.lower(),label)
        self.assertFalse(h.can_handle(bytes(data)))

if __name__ == "__main__":
    unittest.main(verbosity=2)
