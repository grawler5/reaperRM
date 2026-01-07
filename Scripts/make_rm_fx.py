import zipfile, re, textwrap
from pathlib import Path

BASE = Path('/mnt/data/work24')
ZP = Path('/mnt/data/Tukan.zip')
KICKER_SRC = Path('/mnt/data/kicker.txt')

zf = zipfile.ZipFile(ZP)


def read_zip(name: str) -> str:
    data = zf.read(name)
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return data.decode('latin-1')


def strip_gui(text: str) -> str:
    t = text.replace('\r\n', '\n').replace('\r', '\n')
    # remove filenames
    t = re.sub(r'^\s*filename:.*$\n', '', t, flags=re.MULTILINE)
    # strip @gfx and everything after (we want slider-only DAW UI)
    t = re.sub(r'\n@gfx[\s\S]*$', '\n', t, flags=re.MULTILINE)
    return t


def replace_desc_keep_first(text: str, new_desc: str) -> str:
    t = re.sub(r'^desc:.*$', f'desc:{new_desc}', text, flags=re.MULTILINE)
    # If multiple desc: lines exist, keep only the first one
    out = []
    seen = False
    for ln in t.splitlines():
        if ln.startswith('desc:'):
            if not seen:
                out.append(ln)
                seen = True
            else:
                continue
        else:
            out.append(ln)
    return '\n'.join(out) + '\n'


def max_slider_idx(text: str) -> int:
    mx = 0
    for m in re.finditer(r'\bslider(\d+)\b', text):
        mx = max(mx, int(m.group(1)))
    return mx


def insert_before_section(text: str, insert_text: str, section='@init') -> str:
    lines = text.splitlines()
    out = []
    inserted = False
    for ln in lines:
        if (not inserted) and ln.strip().startswith(section):
            out.append(insert_text.rstrip('\n'))
            inserted = True
        out.append(ln)
    if not inserted:
        out.append(insert_text.rstrip('\n'))
    return '\n'.join(out) + '\n'


def ensure_block_and_append(text: str, block_payload: str) -> str:
    if re.search(r'^\s*@block\b', text, flags=re.MULTILINE):
        return text.rstrip('\n') + '\n\n' + block_payload.strip('\n') + '\n'
    return text.rstrip('\n') + '\n\n@block\n' + block_payload.strip('\n') + '\n'


def ensure_init_vars(text: str, vars_lines: str) -> str:
    # Put right after @init line
    lines = text.splitlines()
    out = []
    inserted = False
    for i, ln in enumerate(lines):
        out.append(ln)
        if (not inserted) and ln.strip().startswith('@init'):
            out.append(vars_lines.rstrip('\n'))
            inserted = True
    return '\n'.join(out) + '\n'


def write(name: str, content: str):
    (BASE / name).write_text(content, encoding='utf-8')


DBC_LINE = 'dbc = 20/log(10);'

# ---- RM_LA1A ----
la = strip_gui(read_zip('Tukan/LA-1A (Tukan)'))
la = replace_desc_keep_first(la, 'RM_LA1A [Telemetry]')
la_max = max_slider_idx(la)
la_gr_slider = la_max + 1
la = insert_before_section(la, f'slider{la_gr_slider}:0<0,24,0.01>-Z Telemetry: GR (dB)\n', '@init')
la = ensure_init_vars(la, 'tele_eps = exp(-46.051701859880914);\n')
la = ensure_block_and_append(la, textwrap.dedent(f'''
  // telemetry update
  gr_db = abs(ratio2db(max(tele_eps, gr_meter2)));
  gr_db > 24 ? gr_db = 24;
  slider{la_gr_slider} = gr_db;
  slider_automate(slider{la_gr_slider});
'''))
write('RM_LA1A.jsfx', la)

# ---- RM_Deesser ----
ds = strip_gui(read_zip('Tukan/Deesser (Tukan)'))
ds = replace_desc_keep_first(ds, 'RM_Deesser [Telemetry]')
ds_max = max_slider_idx(ds)
ds_gr_slider = ds_max + 1
# add telemetry slider
# Deesser already computes gr_meter2 and sometimes grdb; we compute fresh
if 'ratio2db' not in ds:
    ds = insert_before_section(ds, DBC_LINE + '\nfunction ratio2db(r) ( log(abs(r))*dbc; );\n', '@init')
ds = insert_before_section(ds, f'slider{ds_gr_slider}:0<0,24,0.01>-Z Telemetry: GR (dB)\n', '@init')
ds = ensure_init_vars(ds, 'tele_eps = exp(-46.051701859880914);\n')
ds = ensure_block_and_append(ds, textwrap.dedent(f'''
  // telemetry update
  gr_db = abs(ratio2db(max(tele_eps, gr_meter2)));
  gr_db > 24 ? gr_db = 24;
  slider{ds_gr_slider} = gr_db;
  slider_automate(slider{ds_gr_slider});
'''))
write('RM_Deesser.jsfx', ds)

# ---- RM_Compressor2 ----
cc = strip_gui(read_zip('Tukan/Compressor 2 (Tukan)'))
cc = replace_desc_keep_first(cc, 'RM_Compressor2 [Telemetry]')
cc_max = max_slider_idx(cc)
cc_in = cc_max + 1
cc_sc = cc_max + 2
cc_out = cc_max + 3
cc = insert_before_section(cc, textwrap.dedent(f'''
slider{cc_in}:0<0,1,0.0001>-Z Telemetry: In Peak
slider{cc_sc}:0<0,1,0.0001>-Z Telemetry: Sidechain Peak
slider{cc_out}:0<0,1,0.0001>-Z Telemetry: Out Peak
'''), '@init')
# init pk vars
cc = ensure_init_vars(cc, 'pkIn=0; pkSC=0; pkOut=0;\n')
# inject @sample peak capture (begin and end)
cc = cc.replace('@sample\n', '@sample\n// telemetry peak capture\npkIn = max(pkIn, max(abs(spl0), abs(spl1)));\npkSC = max(pkSC, max(abs(spl2), abs(spl3)));\n')
# after main output assignment, near end of @sample, add pkOut
# safest: add before first blank line preceding "gr =" (exists)
cc = cc.replace('\n\n\ngr = db2ratio(cL);', '\n// telemetry: output peak\npkOut = max(pkOut, max(abs(spl0), abs(spl1)));\n\n\ngr = db2ratio(cL);')
# add @block for sending and reset
cc = ensure_block_and_append(cc, textwrap.dedent(f'''
  slider{cc_in} = pkIn;
  slider{cc_sc} = pkSC;
  slider{cc_out} = pkOut;
  pkIn = 0; pkSC = 0; pkOut = 0;
  slider_automate(slider{cc_in});
  slider_automate(slider{cc_sc});
  slider_automate(slider{cc_out});
'''))
write('RM_Compressor2.jsfx', cc)

# ---- RM_DelayMachine ----
dm = strip_gui(read_zip('Tukan/Delaymachine (Tukan)'))
dm = replace_desc_keep_first(dm, 'RM_DelayMachine')
# fix import path if present
dm = dm.replace('import DELAYgui/delay-utils.jsfx-inc', 'import delay-utils.jsfx-inc')
write('RM_DelayMachine.jsfx', dm)

# ---- RM_EQT1A ----
eqt = strip_gui(read_zip('Tukan/EQT-1A (Tukan)'))
eqt = replace_desc_keep_first(eqt, 'RM_EQT1A')
write('RM_EQT1A.jsfx', eqt)

# ---- RM_Lexikan2 ----
lex = strip_gui(read_zip('Tukan/Lexikan 2 (Tukan)'))
lex = replace_desc_keep_first(lex, 'RM_Lexikan2')
# remove ui-lib import and ui_setup call
lex = re.sub(r'^\s*import\s+ui-lib\.jsfx-inc\s*$\n?', '', lex, flags=re.MULTILINE)
lex = lex.replace('freemem = ui_setup(0);', 'freemem = 0;')
write('RM_Lexikan2.jsfx', lex)

# ---- RM_Limiter2 ----
lim = strip_gui(read_zip('Tukan/Limiter 2 (Tukan)'))
lim = replace_desc_keep_first(lim, 'RM_Limiter2 [Telemetry]')
lim_max = max_slider_idx(lim)
# add input and maximizer after existing sliders
lim_in = lim_max + 1
lim_maxim = lim_max + 2
lim_tin = lim_max + 3
lim_tout = lim_max + 4
lim_tgr = lim_max + 5
lim = insert_before_section(lim, textwrap.dedent(f'''
slider{lim_in}:0<-24,24,0.1>-Input (dB)
slider{lim_maxim}:0<0,1,1{{Off,On}}>-Maximizer
slider{lim_tin}:0<0,1,0.0001>-Z Telemetry: In Peak
slider{lim_tout}:0<0,1,0.0001>-Z Telemetry: Out Peak
slider{lim_tgr}:0<0,24,0.01>-Z Telemetry: GR (dB)
'''), '@init')
# init pk vars
lim = ensure_init_vars(lim, 'pkIn=0; pkOut=0; tele_eps = exp(-46.051701859880914);\n')
# in @slider, compute input/output gains and optional maximizer behavior
# Find line 'output = 10^(slider4/20);' and replace with extended
lim = lim.replace('output = 10^(slider4/20);', textwrap.dedent(f'''
output = 10^((slider4 - slider{lim_in})/20); // output trim compensates input
inGain = 10^((slider{lim_in})/20);
maxMode = slider{lim_maxim};
''').strip('\n'))

# In @sample, apply inGain (and small makeup in max mode), compute peaks, auto release, softclip
# Insert right after inL/inR lines
lim = lim.replace('inL = spl0;\ninR = spl1;', textwrap.dedent('''
inL = spl0;
inR = spl1;
// telemetry input peak
pkIn = max(pkIn, max(abs(inL), abs(inR)));

// input gain (maximizer adds ~1 dB makeup)
mg = (maxMode > 0.5) ? 10^(1/20) : 1;
inL *= inGain * mg;
inR *= inGain * mg;
''').strip('\n'))

# After computing cG and cL, before final spl assignment, add auto-release tweak in max mode
# We locate line 'cG = smoothAverage(yG, alphaR);' and inject dynamic alphaR
lim = lim.replace('cG = smoothAverage(yG, alphaR);', textwrap.dedent('''
// auto-release in maximizer mode (faster on small GR, slower on heavy GR)
maxMode > 0.5 ? (
  grAbs = abs(yR); // yR holds smoothed gain reduction (dB, negative)
  rel_ms = 30 + min(400, grAbs*18);
  rel_s = rel_ms/1000;
  alphaR_dyn = rel_s>0 ? exp(-1 / (rel_s * srate)) : 0;
  cG = smoothAverage(yG, alphaR_dyn);
) : (
  cG = smoothAverage(yG, alphaR);
);
''').strip('\n'))

# Now, after final spl0/spl1 assignment, add pkOut update and GR telemetry and softclip when max
# We'll insert after 'spl1 = inR * cL * ceiling * output;' line
lim = lim.replace('spl1 = inR * cL * ceiling * output;', textwrap.dedent('''
spl1 = inR * cL * ceiling * output;

// optional soft-clip in maximizer mode
maxMode > 0.5 ? (
  spl0 = tanh(spl0);
  spl1 = tanh(spl1);
);

// telemetry output peak
pkOut = max(pkOut, max(abs(spl0), abs(spl1)));

// GR telemetry in dB (0..24)
gr_db = abs(cG);
gr_db > 24 ? gr_db = 24;
''').strip('\n'))

# Add @block with telemetry send and reset (Limiter 2 didn't have @block)
lim = ensure_block_and_append(lim, textwrap.dedent(f'''
  slider{lim_tin} = pkIn;
  slider{lim_tout} = pkOut;
  slider{lim_tgr} = gr_db;
  pkIn = 0; pkOut = 0;
  slider_automate(slider{lim_tin});
  slider_automate(slider{lim_tout});
  slider_automate(slider{lim_tgr});
'''))
write('RM_Limiter2.jsfx', lim)

# ---- RM_Kicker50hz ----
k = KICKER_SRC.read_text(encoding='utf-8')
k = k.replace('\r\n','\n').replace('\r','\n')
# Keep only first desc and rename
k = replace_desc_keep_first(k, 'RM_Kicker50hz [Telemetry]')
# Add telemetry slider
k_max = max_slider_idx(k)
k_tout = k_max + 1
k = insert_before_section(k, f'slider{k_tout}:0<0,1,0.0001>-Z Telemetry: Out Peak\n', '@init')
# init pk var
k = ensure_init_vars(k, 'pkOut = 0;\n')
# capture output peak after processing lines (after spl0/spl1 assignments)
# We'll insert after 'spl1 = sin(pos)*gain*wet + spl1*dry;' line
k = k.replace('spl1 = sin(pos)*gain*wet + spl1*dry;', 'spl1 = sin(pos)*gain*wet + spl1*dry;\n\n// telemetry out peak\npkOut = max(pkOut, max(abs(spl0), abs(spl1)));')
# add @block send
k = ensure_block_and_append(k, textwrap.dedent(f'''
  slider{k_tout} = pkOut;
  pkOut = 0;
  slider_automate(slider{k_tout});
'''))
write('RM_Kicker50hz.jsfx', k)

# ---- copy required includes ----
(BASE / 'delay-utils.jsfx-inc').write_text(read_zip('Tukan/delay-utils.jsfx-inc').replace('\r\n','\n'), encoding='utf-8')
(BASE / 'smoother.jsfx-inc').write_text(read_zip('Tukan/smoother.jsfx-inc').replace('\r\n','\n'), encoding='utf-8')

print('OK: generated RM_* JSFX files')
