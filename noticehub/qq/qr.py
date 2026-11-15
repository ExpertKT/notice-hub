"""Small dependency-free QR byte-mode encoder (ECC M, versions 1-10)."""
from __future__ import annotations
import io
from PIL import Image

# (version, total codewords, data codewords, block count)
_TABLE=((1,26,16,((1,26,16),)),(2,44,28,((1,44,28),)),(3,70,44,((1,70,44),)),(4,100,64,((2,50,32),)),(5,134,86,((2,67,43),)),(6,172,108,((4,43,27),)),(7,196,124,((4,49,31),)),(8,242,154,((2,60,38),(2,61,39))),(9,292,182,((3,58,36),(2,59,37))),(10,346,216,((4,69,43),(1,70,44))))
_GEXP=[0]*512; _GLOG=[0]*256; x=1
for i in range(255): _GEXP[i]=x; _GLOG[x]=i; x<<=1; x^=(0x11d if x&0x100 else 0)
for i in range(255,512): _GEXP[i]=_GEXP[i-255]
def _mul(a,b): return 0 if not a or not b else _GEXP[_GLOG[a]+_GLOG[b]]
def _ecc(data,n):
    gen=[1]
    for i in range(n):
        out=[0]*(len(gen)+1)
        for j,v in enumerate(gen): out[j]^=v; out[j+1]^=_mul(v,_GEXP[i])
        gen=out
    rem=[0]*n
    for v in data:
        f=v^rem[0]; rem=rem[1:]+[0]
        for j,g in enumerate(gen[1:]): rem[j]^=_mul(g,f)
    return rem
def _bits(v,n): return [(v>>(n-1-i))&1 for i in range(n)]
def _penalty(a):
    n=len(a); p=0
    for rows in (a, list(zip(*a))):
        for r in rows:
            run=1
            for i in range(1,n):
                if r[i]==r[i-1]: run+=1
                else:
                    if run>=5:p+=3+run-5
                    run=1
            if run>=5:p+=3+run-5
            s=''.join('1' if x else '0' for x in r)
            p+=40*(s.count('1011101'))
    for y in range(n-1):
        for x in range(n-1):
            if a[y][x]==a[y+1][x]==a[y][x+1]==a[y+1][x+1]:p+=3
    dark=sum(map(sum,a)); p+=10*abs(dark*100//(n*n)-50)//5
    return p
def _format(m):
    v=(0<<3)|m; g=0x537
    for _ in range(10): v=(v<<1)^ (g if v&0x200 else 0)
    return ((0<<3)|m)<<10 ^ v ^ 0x5412
def _draw(base,data,mask):
    n=len(base); a=[r[:] for r in base]; bit=0; col=n-1; up=True
    while col>0:
        if col==6: col-=1
        ys=range(n-1,-1,-1) if up else range(n)
        for y in ys:
            for x in (col,col-1):
                if a[y][x] is None:
                    z=data[bit] if bit<len(data) else 0; bit+=1
                    flip=((y+x)%2==0,(y%2==0),(x%3==0),((y+x)%3==0),(y//2+x//3)%2==0,(y*x)%2+(y*x)%3==0,((y*x)%2+(y*x)%3)%2==0,((y*x)%3+(y+x)%2)%2==0)[mask]
                    a[y][x]=bool(z ^ flip)
        up=not up; col-=2
    f=_format(mask)
    for i in range(15):
        b=bool((f>>i)&1)
        if i<6: a[i][8]=b
        elif i<8: a[i+1][8]=b
        else: a[n-15+i][8]=b
        if i<8: a[8][n-1-i]=b
        elif i==8: a[8][7]=b
        else: a[8][14-i]=b
    a[n-8][8]=True
    return a
def matrix(text: str, *, ecc: str="M", _mask: int | None = None) -> list[list[bool]]:
    """Return a QR matrix; `_mask` is a test-only forced-mask switch."""
    if ecc.upper()!="M": raise ValueError("仅支持 ECC M")
    raw=text.encode('utf-8')
    row=next((r for r in _TABLE if 4+(8 if r[0]<10 else 16)+len(raw)*8<=r[2]*8),None)
    if not row: raise ValueError("文本过长（超出本编码器支持的最大二维码版本容量）")
    ver,total,dc,groups=row; blocks=sum(g[0] for g in groups); payload=_bits(4,4)+_bits(len(raw),8 if ver<10 else 16)+sum((_bits(b,8) for b in raw),[])
    payload += [0]*(min(4,dc*8-len(payload))); payload += [0]*((-len(payload))%8)
    code=[sum(payload[i+j]<<(7-j) for j in range(8)) for i in range(0,len(payload),8)]
    code += [0xEC if i%2==0 else 0x11 for i in range(dc-len(code))]; chunks=[]; pos=0
    for count, total_len, data_len in groups:
        for _ in range(count): chunks.append(code[pos:pos+data_len]); pos += data_len
    eccs=[_ecc(c,total_len-len(c)) for c,(count,total_len,data_len) in zip(chunks,[g for g in groups for _ in range(g[0])])]
    stream=[]
    for i in range(max(map(len,chunks))): stream += [c[i] for c in chunks if i<len(c)]
    for i in range(max(map(len,eccs))): stream += [c[i] for c in eccs if i<len(c)]
    bits=sum((_bits(x,8) for x in stream),[]); n=17+4*ver; base=[[None]*n for _ in range(n)]
    def finder(x,y):
        for dy in range(-1,8):
            for dx in range(-1,8):
                if 0<=x+dx<n and 0<=y+dy<n: base[y+dy][x+dx]=(0<=dx<=6 and 0<=dy<=6 and (dx in (0,6) or dy in (0,6) or (2<=dx<=4 and 2<=dy<=4)))
    finder(0,0); finder(n-7,0); finder(0,n-7)
    for i in range(8,n-8): base[6][i]=base[i][6]=(i%2==0)
    if ver >= 2:
        pos={2:[6,18],3:[6,22],4:[6,26],5:[6,30],6:[6,34],7:[6,22,38],8:[6,24,42],9:[6,26,46],10:[6,28,50]}[ver]
        for y in pos:
            for x in pos:
                if (y,x) not in {(6,6),(6,n-7),(n-7,6)}:
                    for dy in range(-2,3):
                        for dx in range(-2,3): base[y+dy][x+dx]=(abs(dx)==2 or abs(dy)==2 or (dx==0 and dy==0))
    if ver >= 7:
        v=ver<<12
        while v.bit_length()>12: v^=0x1f25<<(v.bit_length()-13)
        v=(ver<<12)|v
        for i in range(18):
            bit=bool((v>>i)&1)
            base[i//3][i%3+n-11]=bit; base[i%3+n-11][i//3]=bit
    for i in range(15):
        if i<6: base[i][8]=False
        elif i<8: base[i+1][8]=False
        else: base[n-15+i][8]=False
        if i<8: base[8][n-1-i]=False
        elif i==8: base[8][7]=False
        else: base[8][14-i]=False
    base[n-8][8]=True
    candidates=[_draw(base,bits,m) for m in range(8)] if _mask is None else [_draw(base,bits,_mask)]; return min(candidates,key=_penalty)
def png_bytes(text: str, *, scale: int=6, border: int=4) -> bytes:
    if scale<1 or border<0: raise ValueError("scale 必须为正数，border 不能为负数")
    m=matrix(text); n=len(m)+2*border; im=Image.new("1",(n*scale,n*scale),1); pix=im.load()
    for y,row in enumerate(m):
        for x,v in enumerate(row):
            if v:
                for yy in range((y+border)*scale,(y+border+1)*scale):
                    for xx in range((x+border)*scale,(x+border+1)*scale): pix[xx,yy]=0
    out=io.BytesIO(); im.save(out,"PNG"); return out.getvalue()
