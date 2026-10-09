import io, json, unittest
from pathlib import Path
from PIL import Image
from qq_live_digest.qr import matrix,png_bytes
GOLDEN=json.loads((Path(__file__).with_name('qr_golden.json')).read_text(encoding='utf-8'))['cases']
class QRTests(unittest.TestCase):
 def test_shape_finders(self):
  m=matrix('https://x'); n=len(m); self.assertEqual(n,21); self.assertTrue(m[0][0] and m[3][3] and m[n-1][0])
 def test_png_size_and_magic(self):
  b=png_bytes('hello',scale=4,border=4); self.assertTrue(b.startswith(b'\x89PNG')); im=Image.open(io.BytesIO(b)); self.assertEqual(im.size,((len(matrix('hello'))+8)*4,)*2)
 def test_unicode(self): self.assertTrue(matrix('中文二维码'))
 def test_real_subscription_url_and_minimal_version(self):
  url='webcal://demo-machine.demo-tailnet.ts.net/notice.ics?token=NH-TEST-ONLY-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
  self.assertGreaterEqual(len(matrix(url)),4*6+17); self.assertEqual(len(matrix('x')),21)
 def test_long_rejected(self):
  with self.assertRaises(ValueError): matrix('x'*1000)
 def test_golden_matrices(self):
  for case in GOLDEN:
   kw={} if case['forced_mask'] is None else {'_mask':case['forced_mask']}
   self.assertEqual([''.join('1' if x else '0' for x in row) for row in matrix(case['text'],**kw)],case['matrix'],case['label'])
 def test_v1_function_area_and_padding_contract(self):
  self.assertEqual(len(matrix('x'*14)),21)
  self.assertEqual(len(matrix('x'*15)),25)
  import inspect
  src=inspect.getsource(__import__('qq_live_digest.qr',fromlist=['x']).matrix)
  self.assertIn('0xEC',src); self.assertIn('0x11',src)
 def test_encoder_capacity_boundary_is_explicit(self):
  self.assertEqual(len(matrix('a'*213)),57)
  with self.assertRaises(ValueError): matrix('a'*214)
  with self.assertRaises(ValueError): matrix('a'*512)
 def test_version_information_bits_match_spec_table(self):
  # ISO/IEC 18004 Table D.1: 18-bit version information, versions 7-10
  known={7:(110,0x07C94),8:(130,0x085BC),9:(160,0x09A99),10:(200,0x0A4D3)}
  for ver,(size,word) in known.items():
   m=matrix('a'*size); n=len(m); self.assertEqual((n-17)//4,ver)
   self.assertEqual(sum(int(m[i//3][n-11+i%3])<<i for i in range(18)),word,f'top-right version info v{ver}')
   self.assertEqual(sum(int(m[n-11+i%3][i//3])<<i for i in range(18)),word,f'bottom-left version info v{ver}')
 def test_alignment_patterns_are_drawn_at_every_spec_position(self):
  # v7 coordinates 6/22/38: a pattern at every combination except the three overlapping the finders
  m=matrix('a'*110); pos=(6,22,38)
  for y in pos:
   for x in pos:
    if (y,x) in {(6,6),(6,38),(38,6)}: continue
    for dy in range(-2,3):
     for dx in range(-2,3):
      want=abs(dx)==2 or abs(dy)==2 or (dx==0 and dy==0)
      self.assertEqual(bool(m[y+dy][x+dx]),want,f'alignment ({y},{x}) offset ({dy},{dx})')
if __name__=='__main__': unittest.main()
