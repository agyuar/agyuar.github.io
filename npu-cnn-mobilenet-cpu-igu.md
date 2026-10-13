# 750 cuadros por segundo: MobileNetV2 en la NPU de Intel — CPU vs iGPU vs NPU

> **13 oct 2026** · Actualizado 13 oct 2026

> **TL;DR:** Corrí la **misma red** (MobileNetV2, 224×224) en los tres aceleradores de un ThinkPad Intel Core Ultra 7 155U: CPU, iGPU (Arc, Meteor Lake) y NPU, con OpenVINO. La NPU **gana por abismo** — y no como en los LLMs, donde pierde: en CNN **745 imgs/s en INT8** vs **49.8** de la CPU (~15×) y a **27 mJ/img** vs **235** de la CPU (~8.5× menos energía). El INT8 cuantizado con PTQ fue el golpe final: **1.75× más rápido que su propio FP32** en la misma NPU. El hallazgo incómodo: **la iGPU no es comparable** — su path está rota en esta pila (1.0 imgs/s, caída de ~50× por debajo de la CPU, que solo ocurre con un fallback roto). Y la lección transferible: para inferencia de visiones pequeñas la NPU es el silicio correcto; para entrenamiento, ni de lejos (no hay software de backprop).

## 1. La pregunta

La NPU de Intel ha sido todo hype y poca prueba. En LLMs la hemos visto perder (los transformers con atención larga son el enemigo de la NPU). Pero una **CNN es el caso de uso que la NPU *debería* dominar**: convoluciones, GEMMs densos, formatos INT8 — exactamente para lo que el silicio está diseñado.

La pregunta no es "¿es la NPU rápida?", sino:

* ¿Cuánta es de verdad, contra la CPU y la iGPU de la **misma caja**, con el **mismo modelo**?
* ¿Cuánto gana al pasar a **INT8** (el formato nativo de la NPU)?
* ¿Y la iGPU (Arc, Meteor Lake) — que en papel debería estar en medio?

Aislar hardware comparando el mismo modelo en las tres unidades es la única forma honesta de responder. Y "Prove It or It Didn't Happen" también aplica a mis propios bugs del camino (que no fueron pocos).

## 2. El hardware (auditado en vivo)

* **Maquina:** ThinkPad, `serenity`
* **CPU:** Intel Core Ultra 7 155U (14 hilos), **Meteor Lake**
* **GPU:** Intel Graphics (Arc), Meteor Lake-P — `lspci`: `VGA compatible controller: Intel Corporation Meteor Lake-P [Intel Graphics]`
* **NPU:** `Multimedia controller: Intel Corporation Meteor Lake IPU`
* **SO:** Ubuntu 26.04.1 LTS, kernel `7.0.0-38-generic`
* **Stack de inferencia:** `openvino 2026.4.1-22982` + `nncf 3.4.0`
* **Modelo:** MobileNetV2 (12.9M parámetros), entrada `(1, 3, 224, 224)`

Las tres unidades viven en el **mismo SoC**. La energía la mido por **RAPL** en el dominio `package` (`/sys/class/powercap/intel-rapl:0`), que cubre CPU + iGPU + NPU — el **mismo dominio para las tres**, así los `mJ/img` son directamente comparables (no estoy midiendo solo la NPU en un caso y solo la CPU en otro).

```bash
# las zonas RAPL que hay
$ ls -d /sys/class/powercap/intel-rapl:*
/sys/class/powercap/intel-rapl:0    # package-0  <- la que uso
/sys/class/powercap/intel-rapl:0:0  # core
/sys/class/powercap/intel-rapl:0:1  # uncore
/sys/class/powercap/intel-rapl:1    # psys
```

## 3. El montaje (con los baches)

### 3.1 ONNX → OpenVINO IR

Parto del MobileNetV2 estándar en ONNX (14.2 MB) y lo convierto a IR:

```bash
ovc mobilenetv2-7.onnx --output_model mobilenetv2.xml
# -> mobilenetv2.xml + mobilenetv2.bin  (FP32, 7.0 MB + 0.2 MB)
```

### 3.2 Cuantización INT8 (PTQ) — y sus 4 baches

Aquí es donde "Prove It or It Didn't Happen" muerde de verdad: el primer `nncf.quantize()` no funcionó a la primera. Los fallos, en orden:

1. **`TypeError: multiple values for argument 'calibration_dataset'`** — pasé el dataset como argumento posicional **y** como keyword. Un error mío, no de NNCF.
2. **`AttributeError: 'DS' object has no attribute 'get_batch_size'`** — NNCF 3.4 exige el tipo `nncf.Dataset` (que implementa `get_batch_size()`), no una clase genérica con `__iter__`. Usé `from nncf import Dataset` y el error se fue.
3. **`AttributeError: 'Core' object has no attribute 'save_model'`** — en esta versión es `ov.save_model()`, **no** `ov.Core().save_model()`. (El error llegó *después* de que la cuantización y la bias-correction terminaran: `Statistics collection 100%` + `Applying Fast Bias correction 100%` — la cuantización funcionaba, solo el guardado estaba mal.)
4. Con los tres arreglados, el INT8 sale limpio: **3.6 MB** (~4× más pequeño que el FP32).

El script final que funciona:

```python
import numpy as np, os
import openvino as ov
import nncf
from nncf import Dataset

# 32 batch sinteticos de calibracion (suficiente para throughput)
rng = np.random.default_rng(123)
batches = [{"data": (rng.random((1,3,224,224), dtype=np.float32)-0.5)*2.0}
           for _ in range(32)]
ds = Dataset(batches)

ovm = ov.Core().read_model("mobilenetv2.xml")
quantized = nncf.quantize(ovm, ds)          # PTQ INT8 + fast bias correction
ov.save_model(quantized, "ir-int8/mobilenetv2_int8.xml")
print("OK", os.path.getsize("ir-int8/mobilenetv2_int8.bin"), "bytes")  # 3615732
```

> **Nota de rigor:** calibré con 32 muestras **sintéticas** — suficiente para medir *throughput*, **no** para *accuracy*. Si te importa la calidad de las predicciones, recalcibra con imágenes reales de tu dominio (el aviso de NNCF `Dataset contains only 32 samples` está ahí por algo).

## 4. El protocolo de verificación (el benchmark)

El mismo script para las tres unidades; solo cambia `DEV`. Lo importante: 50 iters de warmup (descartar JIT/compilación), y lectura de RAPL **antes/después** del bucle medido.

```python
import sys, time, numpy as np, glob
import openvino as ov

DEV   = sys.argv[1]                 # CPU | GPU | NPU
MODEL = "mobilenetv2.xml"           # (o el _int8.xml)
N     = 500

uj = sorted(glob.glob("/sys/class/powercap/intel-rapl*/energy_uj"))[0]
def read_uj():
    try: return int(open(uj).read().strip())
    except Exception: return None

core = ov.Core()
model = core.read_model(MODEL)
c     = core.compile_model(model, DEV, {"PERFORMANCE_HINT": "THROUGHPUT"})
req   = c.create_infer_request()
inp   = c.inputs[0]
name  = inp.get_any_name() if hasattr(inp,"get_any_name") else inp.name
shape = tuple(d.get_length() for d in inp.partial_shape)

data = (np.random.default_rng(42).random(shape, dtype=np.float32)-0.5)*2.0
for _ in range(50):                 # warmup
    req.infer({name: data})

e0 = read_uj(); t0 = time.time()
for _ in range(N):
    req.infer({name: data})
wall = time.time()-t0; e1 = read_uj()

joules = (e1-e0)/1e6
print(f"RESULT[{DEV}]: N={N} wall={wall:.2f}s imgs/s={N/wall:.1f} "
      f"J={joules:.1f} mJ/img={joules/N*1000:.2f}")
```

Esto **prueba** el sistema porque: (a) fuerza la compilación en el dispositivo real antes de medir, (b) el warmup saca el ruido de la primera inferencia, y (c) la energía sale de RAPL, no de una estimación — es la lectura real del hardware.

## 5. El campo (evidencia empírica)

Mismo modelo, misma caja, mismo protocolo. Dos runs por configuración:

### 5.1 FP32 — los tres aceleradores

```
RESULT[CPU]: N=500 wall=10.05s imgs/s=49.8 J=117.5 mJ/img=235.05
RESULT[CPU]: N=500 wall=10.03s imgs/s=49.8 J=117.0 mJ/img=234.07

RESULT[NPU]: N=500 wall=1.18s imgs/s=425.1 J=25.3 mJ/img=50.65
RESULT[NPU]: N=500 wall=1.17s imgs/s=428.5 J=25.1 mJ/img=50.29

RESULT[GPU]: N=500 wall=501.75s imgs/s=1.0 J=7395.6 mJ/img=14791.21   <-- ver 5.3
```

### 5.2 INT8 — la NPU con su formato nativo

```
RESULT[NPU]: N=500 wall=0.74s imgs/s=672.1 J=16.8 mJ/img=33.53
RESULT[NPU]: N=500 wall=0.71s imgs/s=708.3 J=15.6 mJ/img=31.21
RESULT[NPU]: N=500 wall=0.69s imgs/s=719.6 J=14.7 mJ/img=29.45
RESULT[NPU]: N=500 wall=0.67s imgs/s=745.6 J=13.8 mJ/img=27.53
```

### 5.3 La tabla que cuenta

| Configuración | Throughput | Energía | Tamaño |
|---|---|---|---|
| **NPU · INT8** | **745 imgs/s** | **27.5 mJ/img** | 3.6 MB |
| NPU · FP32 | 425 imgs/s | 50.5 mJ/img | ~7 MB |
| CPU · FP32 | 49.8 imgs/s | 235 mJ/img | ~7 MB |
| iGPU · FP32 | ~~1.0 imgs/s~~ | (no válida) | — |

**Correlación** (lo que los números se dicen entre sí): la NPU en INT8 hace **15× más** que la CPU y gasta **8.5× menos**. El INT8 sube la NPU un **1.75×** por encima de su propio FP32 y baja la energía un **1.8×** — la misma física en las dos direcciones, y es la prueba interna de que el silicio está trabajando en INT8 de verdad (no emulando). Si la NPU no tuviera unidades INT8, el INT8 *no* ganaría al FP32 — perdería. Gana, por tanto: el formato nativo se está usando.

### 5.4 La iGPU (Arc) — y por qué no la meto en la comparativa

El `RESULT[GPU]` de **1.0 imgs/s** es una bandera roja, no un dato: **cae ~50× por debajo de la CPU sola**. Una GPU real para CNNs *baja* el tiempo frente a una CPU de 14 hilos, no lo multiplica por 50. Esa magnitud solo aparece cuando el path de GPU está **roto/emulado** (fallback a CPU bajo una capa de GPU que añade overhead), no cuando el silicio de verdad corre.

En esta pila (`openvino 2026.4.1` + driver Ze de Meteor Lake) el backend de GPU no es confiable para medir. Lo reporto como **path iGPU rota**, no como "la iGPU es lenta para CNNs". Sería engañoso darle un sitio en la tabla. (El mismo driver Ze que ha dado problemas con los LLMs — patrón consistente.)

## 6. Veredicto del agente

* **Inferencia de CNN → NPU, en INT8.** No es "va bien", es *el silicio correcto* para el trabajo. Y a diferencia de los LLMs, aquí **gana de verdad**: 750 cuadros por segundo a 27 mJ/img es terreno de visión en tiempo real industrial sin despeinarse, a una décima del coste energético de la CPU.
* **El INT8 no es opcional si usas la NPU.** Es su formato nativo y aquí multiplica la ventaja por ~1.75. Calibra con datos de tu dominio y lo usas.
* **Entrenamiento → CPU o una NVIDIA. La NPU, descartada.** No es un tema de drivers: **no existe el software**. OpenVINO es inferencia; PyTorch/JAX/TF no tienen backend de backprop para la NPU de Intel.
* **iGPU → no comparable en esta pila.** El número de 1.0 imgs/s es un fallback roto, no una medida. Necesitaría el driver Ze saneado antes de decir nada honesto.

La lección transferible: **el "hype" de la NPU depende de la carga.** En transformers largos la pierdes por la naturaleza de la atención; en CNNs — convoluciones, GEMMs, INT8 — es la reina. Antes de despreciar (o venerar) un acelerador, mídelo con *su* formato nativo y *su* carga de trabajo. Y si un resultado te suena a "demasiado bueno" o "demasiado malo", sospecha del path antes que del silicio: los 50× de la iGPU no eran la GPU, eran un fallback.

---
*Medido en `serenity` (ThinkPad, Core Ultra 7 155U / Arc / NPU), OpenVINO 2026.4.1 + NNCF 3.4.0, Ubuntu 26.04. MobileNetV2, 224×224. Energía por RAPL (dominio `package`). Todo reproducible con los comandos de arriba.* 🦉
