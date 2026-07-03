**Stabilization logic**

This document summarizes the mathematical steps used in `ejo_wfb_stabilizer.py` to compute image stabilization and how quickly the image returns toward center.

**State and measurements**
- The code uses a 3-element state vector for cumulative motion:

$X = [x, y, a]^T$

where:
- $x, y$ are cumulative translations (pixels) and
- $a$ is cumulative rotation (radians).

- The instantaneous measured frame-to-frame increments come from the affine fit `m`:

$dx = m_{0,2},\quad dy = m_{1,2},\quad da = \operatorname{atan2}(m_{1,0}, m_{0,0})$

The code then integrates these to form the measured cumulative state:

$x \leftarrow x + dx,\quad y \leftarrow y + dy,\quad a \leftarrow a + da$

and constructs the measurement vector

$Z = [x, y, a]$


**Per-component scalar Kalman update (implemented element-wise)**

The script implements a simple discrete-time Kalman-like filter independently on each component. Key variables (per component) are:
- Estimate: $X_{est}$ (1×3 array)
- Estimate covariance: $P$ (1×3)
- Process noise (Q): set as `Q = [[processVar*downSample]*3]`
- Measurement noise (R): set as `R = [[measVar/downSample]*3]`

The predict and update performed in the code (when `count != 0`) are:

Predict:
$$X_{pred} = X_{est}$$
$$P_{pred} = P + Q$$

Gain (element-wise):
$$K = \frac{P_{pred}}{P_{pred} + R}$$

Update:
$$X_{est} = X_{pred} + K \odot (Z - X_{pred})$$
$$P = (1 - K) \odot P_{pred}$$

Here $\odot$ denotes element-wise multiplication. Note: the code initializes `X_est = 0` and `P = 1` on first frame (count == 0).


**How the filter affects the applied transform**

The script computes the difference between the filtered estimate and the raw integrated measurement:

$\Delta = X_{est} - Z = [\Delta_x, \Delta_y, \Delta_a]$

Then it adds these corrections back into the instantaneous transform values used for rendering:

$dx_{corrected} = dx + \Delta_x$
$dy_{corrected} = dy + \Delta_y$
$da_{corrected} = da + \Delta_a$

The 2×3 affine matrix applied to the previous frame is constructed as:

$$m = \begin{bmatrix}\cos(da_{corr}) & -\sin(da_{corr}) & dx_{corr}\\[4pt]
                          \sin(da_{corr}) & \cos(da_{corr}) & dy_{corr}\end{bmatrix}$$

This produces a smoothed, corrected warp: translation and small rotation are applied to the image.


**Settling / return-to-center behaviour**

Treat one scalar component (e.g. $x$) in isolation. The Kalman update for the estimate is a simple exponential smoothing form when the state transition is identity:

$X_{est,new} = X_{est,old} + K\,(Z - X_{est,old}) = (1-K)\,X_{est,old} + K\,Z$

If $Z$ suddenly steps toward zero (or the target), the error $e_n = X_{est} - Z$ evolves per frame as:

$e_{n+1} = (1-K)\,e_n$

Therefore the per-frame decay factor is $(1-K)$. After $n$ frames the remaining error is

$e_n = (1-K)^n e_0$

To reach a fraction $\alpha$ (e.g. $\alpha=0.05$ for 5%) of the initial error requires

$$n = \frac{\ln(\alpha)}{\ln(1-K)}$$

So the speed of return is governed entirely by the Kalman gain $K$ (per component).


**How `processVar`, `measVar` and `downSample` influence speed**

- $Q$ (process noise) = `processVar * downSample`. Increasing `processVar` increases $Q$, which increases $P_{pred}$ and therefore increases $K$ → faster response (larger correction per frame).
- $R$ (measurement noise) = `measVar / downSample`. Increasing `measVar` increases $R$ → smaller $K$ → slower response (smoother but slower to center).
- `downSample` changes both: lowering `downSample` (e.g. 0.5) reduces $Q$ and increases $R$, which typically reduces $K$ and makes the filter slower (more smoothing). Raising `downSample` toward 1 increases $Q$ relative to $R$ and makes the filter react faster.

Concrete examples (using `P=1` initial):
- Default values in code: `processVar=0.03`, `measVar=2`, `downSample=1` →
  - $Q=0.03$, $R=2$, $P_{pred}=1.03$ → $K\approx 1.03/(1.03+2)\approx 0.34$ → per-frame multiplier $(1-K)\approx 0.66$. To reach 5%: $n\approx 7$ frames.
- Fast mode in code uses `downSample=0.5` typically →
  - $Q=0.03*0.5=0.015$, $R=2/0.5=4$, $P_{pred}\approx 1.015$ → $K\approx 1.015/(1.015+4)\approx 0.20$ → $(1-K)\approx 0.80$. To reach 5%: $n\approx 13$ frames (slower).

These numeric examples show how tuning `processVar` upward or `measVar` downward will speed the return-to-center, while the opposite changes make the system smoother but slower.


**Other implementation notes that affect dynamics**
- The code accumulates raw increments into $x,y,a$ before the filter; the filter smooths the cumulative values. Large individual frame jumps are clipped by the `max_windows_offset` check which resets the filter state if a jump is extreme.
- The optical flow / feature-tracking quality (number and stability of `prevPts` / `currPts`) affects measured $dx,dy,da$ and therefore affects how well the filter can track and recentre.
- The code applies an extra rotation/zoom matrix `T` for `zoomFactor` after the affine warp. `zoomFactor` does not directly change the filter, but it changes what part of the image is visible and thus perceived stability.


**Summary**
- The stabilizer implements a per-component 1D Kalman-like exponential smoother. The Kalman gain
$$K = \frac{P+Q}{P+Q+R}$$
controls the responsiveness; the effective per-frame decay of error is $(1-K)$. Adjusting `processVar` or `measVar` (and `downSample`) changes $Q$ and $R$ and hence the settling time.

For further tuning, increase `processVar` to make the system return to center faster, increase `measVar` to make it smoother and slower.

---

References to code: see `ejo_wfb_stabilizer.py` for variable names and the exact update flow.
