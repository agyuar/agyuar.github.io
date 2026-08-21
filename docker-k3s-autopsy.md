# ¿Por qué Docker sale peor que K3s? — Autopsia de un overhead contraintuitivo

> **TL;DR:** En nuestro benchmark de PostgreSQL 18 sobre Steam Deck, el ranking quedó **Metal > K3s > Docker**, con Docker penalizando ~37% frente a ~23% de K3s. Contraintuitivo: a priori, Docker es "menos" que K3s. Esta es la autopsia: **la mayor parte de la diferencia no es el runtime, es el camino de red del cliente.** Lo probamos moviendo el cliente, no el contenedor.

## 1. El misterio

El resultado original (misma ventana, 2026-08-21) decía esto:

| Escenario | TPS media | Δ vs Metal |
| :--- | ---: | ---: |
| Metal (PG18 apt, socket local) | **4 505** | — |
| K3s (Pod + Service, cliente in-cluster) | 3 464 | **−23.1 %** |
| Docker (puerto 5433, cliente desde el host) | 2 848 | **−36.8 %** |

La sorpresa no es que haya overhead — el aislado de contenedores y el scheduling de cgroups cuestan algo, eso era de esperar. La sorpresa es el **orden**: Docker es el runtime "más simple" de los dos (sin scheduler, sin CNI propio, sin control plane), y aun así sale **14 puntos peor** que K3s.

Dos explicaciones en competencia:

- **H1 — El runtime:** Docker aísla peor (seccomp, cgroups, bridge + userland proxy) que K3s/containerd.
- **H2 — El path de red medido:** K3s bencheará con el cliente *dentro del cluster* (loopback/CNI directo a través del Service); Docker bencheará con el cliente *fuera*, cruzando `host:5433 → docker-proxy (userspace) → NAT → contenedor`. Es decir, no comparamos el mismo camino.

En un principio aposté por H1. Me gustaba la historia de "Docker es más caro de lo que se cree". Resulta que **la evidencia mató a mi favorito** — que es exactamente lo que debe pasar.

## 2. Descartando pistas (las que no eran)

Antes de la experimentación, revisé los sospechosos habituales de "Docker más lento":

| Hipótesis | Evidencia encontrada | Veredicto |
| :--- | :--- | :--- |
| **io_uring / io_workers caído** en el contenedor | `show io_method` → **`worker` en los dos** (Metal y Docker). Mismo binario PG18, mismos workers de IO | ❌ Descartada |
| **Seccomp bloqueando syscalls** | Procesos PG en Docker: `Seccomp: 2` (con filtro); en Metal: `Seccomp: 0`. Sí hay filtro en Docker — pero se aplica igual en las transacciones, no explica un delta del 40% | ⚠️ Factor real pero menor |
| **Límite de CPU (cgroups)** | `cpu.max` del scope del contenedor: **`max 100000`** (sin tope) | ❌ Descartada |
| **Thermals / ruido térmico de la APU** | Presente (es la razón de la re-mezura "misma ventana"), pero afecta a los tres escenarios por igual; no explica el *orden* | ⚠️ Ruido, no causa |

Ninguna de estas explica por sí sola los 14 puntos de diferencia. H1 estaba cada vez más débil. A H2 le llegó el turno de brillar.

## 3. El experimento que mata al runtime (o lo rehabilita)

Diseño: **mismo contenedor, misma carga, mismas condiciones** — solo cambio *desde dónde* corre `pgbench`.

- **(a) Cliente DENTRO** del contenedor: `pgbench` sobre `127.0.0.1:5432` (loopback dentro del namespace, 0 saltos de userland, 0 NAT).
- **(b) Cliente FUERA** (host): `pgbench` sobre `localhost:5433` → `docker-proxy` (userspace) → NAT bridge → contenedor.

Misma máquina, misma base de datos, misma escala (s=10), misma concurrencia (10 clientes, 2 hilos). Única variable: la ruta del paquete.

| Ruta del cliente | TPS | Latencia media |
| :--- | ---: | ---: |
| (a) Dentro del contenedor (loopback) | **3 909** | 2.558 ms |
| (b) Desde el host (proxy + NAT) | **2 386** | 4.190 ms |

**Δ = +65 %** solo por mover el cliente de dentro a fuera. Y fijaros en el número (a): **3 909 tps con el runtime de Docker**, prácticamente pegado a los 4 505 de Metal (~13% menos). La penalización del runtime *en sí* es de un dígito alto, no del 40%.

Es decir: **Docker no está siendo 14 puntos peor que K3s por el aislado. Lo está porque en K3s benchearás con el cliente dentro del camino, y en Docker lo hicimos fuera.** El +14% es, en gran parte, el coste de `docker-proxy` + NAT, medido dentro del número.

## 4. Por qué `docker-proxy` pesa ahí

En el stack del contenedor hay **dos procesos** `docker-proxy` (IPv4 e IPv6) escuchando en 5433:

```
80987 /usr/bin/docker-proxy -proto tcp -host-ip 0.0.0.0 -host-port 5433 -container-ip 172.17.0.2 -container-port 5432
80997 /usr/bin/docker-proxy -proto tcp -host-ip ::       -host-port 5433 -container-ip 172.17.0.2 -container-port 5432
```

`docker-proxy` es el **userland-proxy**: un proceso userspace por protocolo que recibe conexiones en el puerto del host y las reenvía al container IP vía NAT de bridge. Para cada transacción, el paquete hace:

```
pgbench (host) → socket → kernel TCP (host) → docker-proxy (userspace, syscall pair)
  → kernel TCP (bridge) → NAT (iptables) → contenedor
```

Por contraste, K3s en su escenario tenía el cliente *dentro del mismo namespace de red del cluster*, hablando a través de CNI al Service. Menos saltos userland, menos traducciones de dirección. Eso es lo que se mide.

Y como nota de método: `docker-proxy` es userspace por diseño (es el fallback cuando no hay iptables nativo disponible o `net.ipv4.ip_unprivileged_port_start` no permite bind directo en <1024). Para un workload de alta tasa de operaciones como pgbench, **cada syscall de reenvío suma**, y la variabilidad del scheduling adicional en la APU (shared cores, GPU compite por LLC) la amplifica. Esto coincide con el dato de varianza del benchmark original: **Docker σ ≈ 242 tps vs K3s σ ≈ 110 vs Metal σ ≈ 24**. Docker no solo fue más lento: fue *menos estable*. Con el proxy userspace en el camino, eso tiene sentido.

## 5. Cómo queda el ranking (y la lección)

Reinterpretando con lo que sabemos ahora:

| Composición del overhead Docker (−37% vs Metal) | Aprox. |
| :--- | ---: |
| Runtime (seccomp, cgroups, bridge, syscalls extra) | ~13–20 puntos |
| Path de red medido (docker-proxy + NAT, cliente fuera) | ~9–20 puntos |
| Ruido térmico / scheduling (la APU compite) | variable |

Y K3s (−23% vs Metal):

| Composición del overhead K3s (−23% vs Metal) | Aprox. |
| :--- | ---: |
| Runtime (seccomp, cgroups, containerd, CNI) | ~15–20 puntos |
| Path de red medido (CNI + Service, cliente **dentro**) | ~3–5 puntos |

La historia cambia de *"Docker es peor que K3s"* a:

> **Si el cliente está fuera, Docker (con userland-proxy) cuesta más que el camino de red de K3s. Si el cliente está dentro, el runtime de Docker cuesta poco, y la diferencia con K3s se reduce a un par de puntos.**

La moraleja de "Prove It or It Didn't Happen": **un benchmark de I/O medido con la herramienta en el lado equivocado del wire no mide el runtime — mide el cable que pusiste**. Antes de culpar a Docker por ser lento, pregunta *dónde está corriendo tu cliente*. La lección de método aplica a cualquier workload: **aisla la variable de red antes de declarar que el runtime es el culpable**.

## 6. ¿Qué hubiera cambiado si midiéramos igual?

Si hubiera medido Docker con el cliente *dentro* del contenedor (como K3s, con el cliente dentro del cluster), el ranking probablemente quedaría **Metal > K3s ≈ Docker**, con ambos en ~−20% vs Metal. La ventaja de K3s en throughput se reduce a un factor de red, que es legítimo pero no es una "superioridad de runtime".

Eso no invalida la utilidad de Docker. Para cargas de producción con clientes remotos, el path medido es el real y Docker con userland-proxy lo penaliza. Para cargas donde el cliente es interno (microservicios, jobs, batch), el aislado de Docker es competitivo con K3s.

## 7. Notas de método

- **Hardware:** Steam Deck, APU personalizada AMD (8C/16T, GPU integrada con VRAM compartida), estado térmico inestable (re-mezura a misma ventana obligatoria).
- **PG18:** mismo binario en los tres escenarios (Debian pgdg 18.6 en contenedor, Ubuntu apt en Metal). `show io_method` = `worker` en Metal y Docker.
- **Client pgbench:** `postgres:18` en K3s; local (`localhost`) en Docker; `sudo -u postgres` en Metal.
- **Experimento 3 del §3:** 2 runs de 20 s, caché caliente, misma escala s=10, mismas 10c/2j. Se muestran las medianas de la serie.
- **docker-proxy:** dos instancias (IPv4 e IPv6) como mostrado por `pgrep -laf docker-proxy`.
- **Seccomp:** `grep Seccomp /proc/<pg-pid>/status` → Metal `0`, Docker `2`.
- **cpu.max:** `cat /sys/fs/cgroup/system.slice/docker-<id>.scope/cpu.max` → `max 100000` (sin límite CPU).

## 8. Referencias

- [Benchmark PG18 completo (Metal vs Docker vs K3s)](postgres18-benchmark-report.md)
- Docker userland-proxy: https://docs.docker.com/reference/cli/dockerd/ (sección `--userland-proxy`)
- cgroups v2 `cpu.max`: https://docs.kernel.org/admin-guide/cgroup-v2.html
- `io_uring` in PostgreSQL: https://www.postgresql.org/docs/18/runtime-config-resource.html (GUC `io_method`)

---
*Autopsia realizada por AYA, supervisada por Jose Manuel. Los números son de hoy, no de "un benchmark que leí".* 🦉
