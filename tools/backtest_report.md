# HSW backtest report

Generated: 2026-06-04T20:06:10Z
Source: github.com/Implex-ltd/hcaptcha-reverse/tree/main/archive/hsw

**Summary**: 0 / 12 archives are structurally identifiable by the modern extractor (WASM extracted + vc export + >=1 KS candidate + >=2 magic/if/fixslice triples).

| version | wasm KB | funcs | vc export | max-arg void export | AES KS candidates | magic+if+fixslice triples | distinct KS called | identifiable |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1_39_0 | 319.2 | 535 | no | _dyn_core_...1810cf62(4) | 1 | 0 | 0 | FAIL |
| 1_40_0 | 319.2 | 535 | no | _dyn_core_...1810cf62(4) | 1 | 0 | 0 | FAIL |
| 1_40_13 | 322.4 | 535 | no | _dyn_core_...70f55179(4) | 1 | 0 | 0 | FAIL |
| 1_40_14 | 322.4 | 535 | no | _dyn_core_...70f55179(4) | 1 | 0 | 0 | FAIL |
| 1_40_15 | 322.7 | 535 | no | _dyn_core_...70f55179(4) | 1 | 0 | 0 | FAIL |
| 1_40_16 | 281.9 | 271 | no | __wbindgen_export_3(4) | 2 | 0 | 0 | FAIL |
| 1_40_21 | 277.0 | 271 | no | cb(4) | 2 | 0 | 0 | FAIL |
| 1_40_30 | 277.2 | 271 | no | eb(4) | 2 | 0 | 0 | FAIL |
| 1_40_31 | 278.9 | 277 | no | fb(4) | 2 | 0 | 0 | FAIL |
| 1_40_32 | 278.9 | 277 | no | fb(4) | 2 | 0 | 0 | FAIL |
| 1_40_33 | 281.1 | 280 | no | gb(4) | 2 | 0 | 0 | FAIL |
| 1_40_34 | 281.2 | 280 | no | gb(4) | 2 | 0 | 0 | FAIL |

### Failure notes

- **1_39_0**: no dispatcher: largest export takes 4 args (modern HSW vc takes >=8). This pre-dispatcher build needs a different extractor strategy.
- **1_40_0**: no dispatcher: largest export takes 4 args (modern HSW vc takes >=8). This pre-dispatcher build needs a different extractor strategy.
- **1_40_13**: no dispatcher: largest export takes 4 args (modern HSW vc takes >=8). This pre-dispatcher build needs a different extractor strategy.
- **1_40_14**: no dispatcher: largest export takes 4 args (modern HSW vc takes >=8). This pre-dispatcher build needs a different extractor strategy.
- **1_40_15**: no dispatcher: largest export takes 4 args (modern HSW vc takes >=8). This pre-dispatcher build needs a different extractor strategy.
- **1_40_16**: no dispatcher: largest export takes 4 args (modern HSW vc takes >=8). This pre-dispatcher build needs a different extractor strategy.
- **1_40_21**: no dispatcher: largest export takes 4 args (modern HSW vc takes >=8). This pre-dispatcher build needs a different extractor strategy.
- **1_40_30**: no dispatcher: largest export takes 4 args (modern HSW vc takes >=8). This pre-dispatcher build needs a different extractor strategy.
- **1_40_31**: no dispatcher: largest export takes 4 args (modern HSW vc takes >=8). This pre-dispatcher build needs a different extractor strategy.
- **1_40_32**: no dispatcher: largest export takes 4 args (modern HSW vc takes >=8). This pre-dispatcher build needs a different extractor strategy.
- **1_40_33**: no dispatcher: largest export takes 4 args (modern HSW vc takes >=8). This pre-dispatcher build needs a different extractor strategy.
- **1_40_34**: no dispatcher: largest export takes 4 args (modern HSW vc takes >=8). This pre-dispatcher build needs a different extractor strategy.
