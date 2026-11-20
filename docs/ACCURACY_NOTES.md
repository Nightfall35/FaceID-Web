# Accuracy notes for FaceID-Web (ArcFace / buffalo_l, 1:N)

Research notes for Sunny. Scope: a small, consented personal gallery labeled with InsightFace `buffalo_l` (SCRFD-10GF + ResNet50 ArcFace @ WebFace600K) on CPU via onnxruntime. Figures below are sourced; unsourced values are labeled as starting points to tune on Altair's own photos. Retrieved 2026-11-20 (America/Los_Angeles).

Use cosine similarity on L2-normalized embeddings (`face.normed_embedding`; score = dot product). Do not mix cosine similarity with cosine distance, and never reuse a threshold after a model or preprocessing change.

## 1. Open-set 1:N decision rule

This app is open-set identification: a probe may be someone who is not enrolled and must be labeled unknown. A closed-set "always return top-1" rule will mislabel strangers.

InsightFace's own evaluation guide recommends reporting 1:N results as FNIR / FPIR at a frozen threshold, and gives this decision shape ([guide](https://www.insightface.ai/guides/choose-face-recognition-model-and-evaluate)):

1. Detect, 5-point align, embed, L2-normalize the probe.
2. Score cosine similarity against every enrolled template.
3. Take the best score per identity (if an identity has several embeddings).
4. If `top1_score < T`, return **unknown**.
5. Otherwise return the top-1 identity.

Add a second gate so two enrolled people who look alike are not auto-labeled:

- If `top1_score >= T` but `(top1_score - top2_score) < M`, return **ambiguous / unknown** (optionally show both candidates for a human check).

There is no official InsightFace value for the margin `M`. Log top-1 and top-2 on a held-out set of mate and non-mate photos and pick a margin that rejects the confusion cases you care about. A small gap such as 0.05 is only a starting point to tune, not a published operating point.

False accepts get worse as the gallery grows. A threshold that is acceptable for 1:1 verification (one comparison) is not automatically safe for 1:N: each non-mate probe is compared to every enrolled identity, so FPIR tends to rise with `N`. For a household-scale gallery this is milder than a large watchlist, but still use a threshold plus an unknown rule, and re-tune if the gallery grows a lot. See the same InsightFace guide and the distinction between NIST FRVT 1:1 and 1:N.

## 2. Recommended cosine thresholds (starting points)

InsightFace does **not** publish a single official 1:N threshold for `buffalo_l`. Thresholds depend on backbone, training data, demographics, capture quality, and gallery size. Always calibrate on your data: collect genuine (same person) and impostor (different people, including non-enrolled) scores, pick `T` from a validation split at a target false-accept rate, then freeze it and evaluate on a held-out test split ([guide](https://www.insightface.ai/guides/choose-face-recognition-model-and-evaluate)).

Sourced reference points, all cosine similarity unless noted:

| Source | What it is | Value | How to use it |
| --- | --- | --- | --- |
| InsightFace evaluation guide | Typical 1:1 operating band at FMR = 1e-4 to 1e-5 for InsightFace packs | **0.30–0.45** | Reference band only; recompute. The guide's open-set 1:N sample code uses `threshold = 0.40`. |
| InsightFace PersonAnalysis (SDK 2.1) | Default `face_similarity_threshold` | **0.45** | The closest official default for "is this a registered person?". Sensible start for FaceID-Web. |
| Academic pack scores | LFW / IJB-C are 1:1 protocol numbers, not a threshold | See §7 | Do not convert a TAR@FAR number into a production threshold. |

Practical starting policy to tune (not sourced as an official operating point):

- Auto-label if `top1 >= 0.45` and the top-1/top-2 margin clears `M`.
- Optional review band (e.g. 0.35–0.45): show the best candidate, do not auto-label.
- Below that: unknown.

Prefer the stricter end of the 0.30–0.45 band for auto-labeling. False accepts (wrong name on a photo, or a stranger labeled as someone enrolled) are usually worse for this app than a missed identification. If Altair would rather catch more hard poses and accept more mistakes, lower `T` only after looking at real impostor scores.

Third-party defaults disagree and should not be copied blindly. Some apps treat 0.45 as "same person, other pose/light" and 0.60 as strict; others use a cosine-*distance* cutoff near 0.35 (equivalent to similarity 0.65 if distance is `1 - cosine`). Those are not InsightFace operating points.

## 3. Enrollment photos: how many, and what kind

Official InsightFace PersonAnalysis guidance ([person analysis guide](https://github.com/deepinsight/insightface/blob/master/python-package/docs/person_analysis.md)):

- Register **multiple face photos per person to cover different angles**.
- There is **no per-person face-sample limit**; "three or more valid, distinct face samples" are accepted.
- Quality and near-duplicate checks still apply (default duplicate cosine **0.98**).
- Default registration minimums: shorter side of the face box **32 px** (`face_registration_min_size`), detection score **0.6** (`face_min_score`). Faces also need **five valid landmarks** for alignment.
- For probes in a scene, the default minimum box is looser: **20 px** (`face_min_size`). Those are minimums for the SDK to accept a sample, not quality targets.

Practical enrollment recipe (combine the SDK rules with ordinary capture hygiene):

- Aim for **3–10 distinct photos** per person, not 30 near-duplicates. Variety beats quantity.
- Include a clear frontal, slight left/right yaw, a smile/neutral change, indoor and outdoor or otherwise different lighting, and the glasses/hair/beard states the person actually uses.
- Prefer sharp, unoccluded faces that are **much larger than 32 px**. Recognition input is a **112×112** aligned crop ([guide](https://www.insightface.ai/guides/choose-face-recognition-model-and-evaluate); [IJB eval alignment](https://github.com/deepinsight/insightface/blob/master/recognition/arcface_torch/eval_ijbc.py)). A 32 px box will be heavily upscaled; treat 32 px as a hard floor, not a goal.
- Reject for enrollment: no face, multiple faces you cannot disambiguate, `det_score` below 0.6 (or a higher bar you set), missing landmarks, extreme blur, heavy occlusion, or a near-duplicate of an existing sample.
- Optional extra filter: unusually low embedding L2 norm is a known quality proxy (this is the signal AdaFace uses; [paper](https://arxiv.org/abs/2204.00964)). If you use it, calibrate the cutoff on `buffalo_l` outputs; do not copy a cutoff from another model.

## 4. Averaging embeddings vs keeping a gallery of all embeddings

Two standard approaches:

**Multi-template (recommended default for this app).** Store every accepted enrollment embedding for a person. At search time, the person's score is the **max** cosine against their templates. This is what InsightFace PersonAnalysis does: "all valid, distinct face samples are retained" and matched against the reference library. It handles pose and lighting better, because a profile probe can match a profile enrollment instead of a diluted average. For a small gallery the extra storage (512 floats per photo) is negligible.

**Mean template.** Average the L2-normalized embeddings (or the IJB-style media aggregates) and L2-normalize the result, then compare against one vector per person. InsightFace's IJB-C evaluation builds a template by averaging flip features, aggregating media, summing into a template, and L2-normalizing ([`eval_ijbc.py`](https://github.com/deepinsight/insightface/blob/master/recognition/arcface_torch/eval_ijbc.py)). A single mean is compact and can reduce noise when enrollments are all high-quality frontals, but it can wash out pose-specific information.

A hybrid that is reasonable to try, but should be tuned: keep all embeddings, and also store a mean template; score as the max of (mean-template similarity, best single-template similarity). Do not average in low-quality samples; filter first.

Never mix embeddings from different packs (`buffalo_l` vs `antelopev2` vs AdaFace). Re-enroll after a model change.

## 5. Test-time augmentation (horizontal-flip averaging)

InsightFace's official IJB evaluation enables flip test by default (`use_flip_test = True`). It runs the aligned 112×112 crop and its horizontal flip, then **adds** the two feature halves before later L2-normalization and template aggregation ([`eval_ijbc.py`](https://github.com/deepinsight/insightface/blob/master/recognition/arcface_torch/eval_ijbc.py)).

For FaceID-Web, a practical equivalent on CPU:

1. Align to 112×112 as the pack already does.
2. Embed the crop and the horizontally flipped crop.
3. Average the two embeddings (or add them, same thing up to scale) and L2-normalize.

Use the same procedure at enrollment and at probe time. Cost is roughly 2× recognition inference per face (detection and alignment still run once). For a personal photo-labeling app that is usually worth it; if CPU latency becomes painful, keep flip averaging on enrollment and measure whether dropping it at probe time hurts your validation set.

The same IJB script can also weight features by detector "faceness" score (`use_detector_score = True`) and can leave feature-norm information in the vector before aggregation. Those are evaluation knobs, not API defaults. If you experiment with quality-weighted fusion, validate on your photos.

## 6. Pose, lighting, and low-resolution faces

**Alignment.** Always use the pack's 5-point similarity-transform alignment into a 112×112 RGB crop with the pack's mean/std. Mismatched preprocessing is the most common reason published numbers do not reproduce ([guide](https://www.insightface.ai/guides/choose-face-recognition-model-and-evaluate)). Drop faces that lack valid landmarks rather than embedding a bad crop.

**Pose.** `buffalo_l` is already strong on the official CFP-FP frontal-vs-profile benchmark (99.33%; [model zoo](https://github.com/deepinsight/insightface/blob/master/model_zoo/README.md)). Real-world profile, up-tilt, and heavy occlusion still fail more often than frontal shots. Mitigate with multi-pose enrollment, the unknown/margin rule, and by not forcing a label on a poorly aligned face.

**Lighting.** Enroll under more than one lighting condition. Reject obviously blown-out or near-black faces via `det_score` / visual QA. There is no official "lux" cutoff.

**Low resolution and upscaling.** The recognizer always sees 112×112. Upscaling a 20–40 px face cannot invent identity detail. InsightFace's commercial-model guidance flags low resolution under about **48 px inter-pupillary distance** as a hard condition ([guide](https://www.insightface.ai/guides/choose-face-recognition-model-and-evaluate)). For this app:

- Prefer source faces whose box is large enough that the aligned crop is not a heavy upsample.
- Do not rely on generic super-resolution to "fix" identity; if you try it, treat it as an experiment and measure, do not assume a gain.
- For group photos with tiny faces, it is better to return unknown than a confident wrong name.
- Consider a higher minimum size for auto-labeling than for detection (for example, require a larger box than the SDK's 20 px probe floor). Pick that floor on your validation photos.

**Detector input size.** `FaceAnalysis.prepare` detection size (commonly 640) affects which faces are found, especially small ones, and also CPU cost. Changing it can change both recall and the score distribution; re-tune `T` if you change it.

## 7. Do newer open models clearly beat buffalo_l on CPU?

**Short answer: no, not clearly enough to switch by default.** Stay on `buffalo_l` unless you have a measured problem on Altair's photos (especially low-res or extreme pose) and are willing to pay more CPU time, a larger download, and a new threshold/enrollment pass.

Official pack sizes and accuracy from the [InsightFace model zoo](https://github.com/deepinsight/insightface/blob/master/model_zoo/README.md) and [Python model zoo doc](https://github.com/deepinsight/insightface/blob/master/python-package/docs/model_zoo.md):

| Pack | Detector | Recognizer | Size | LFW | CFP-FP | AgeDB-30 | IJB-C (E4) | MR-ALL (MFR) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **buffalo_l** (default) | SCRFD-10GF | R50 @ WebFace600K | **326 MB** | 99.83 | 99.33 | 98.23 | **97.25** | **91.25** |
| buffalo_m | SCRFD-2.5GF | same R50 as buffalo_l | 313 MB | same recognition accuracy as buffalo_l | | | | |
| buffalo_s | SCRFD-500MF | MBF @ WebFace600K | 159 MB | 99.70 | 98.00 | 96.58 | 95.02 | 71.87 |
| antelopev2 | SCRFD-10GF | R100 @ Glint360K (`glintr100`) | **407 MB** | not listed in the pack accuracy table | | | | |

Notes on those zoo numbers:

- `buffalo_m` equals `buffalo_l` on recognition; it only uses a cheaper detector. If CPU is tight and small faces are rare, `buffalo_m` is the first downgrade to try.
- LFW is saturated and only a sanity check ([guide](https://www.insightface.ai/guides/choose-face-recognition-model-and-evaluate)).
- The East-Asian MFR cohort for `buffalo_l` is **74.96**, versus 90.29 African / 94.70 Caucasian / 93.16 South Asian. That gap is large enough to matter if the gallery or probes include East Asian faces; measure on your population rather than trusting LFW.
- Maintainers have said the `buffalo_l` WebFace600K R50 used a different learning-rate policy and can outperform Glint360K R100 on their MFR testing ([issue #1820](https://github.com/deepinsight/insightface/issues/1820)). So **antelopev2 is not automatically more accurate** than `buffalo_l`, despite being an R100.

**antelopev2 / glintr100.** Larger (407 MB vs 326 MB) and slower (R100 vs R50, same SCRFD-10GF detector). A student WIDER FACE timing study reported about **1.44 FPS** for `buffalo_l` vs **0.92 FPS** for `antelopev2` on CPU for their detection-heavy benchmark ([thesis PDF](https://essay.utwente.nl/fileshare/file/110891/ThesisDraft__Copy_-12.pdf)); treat that as one hardware-specific data point, not an official InsightFace number. Recognition accuracy itself should not change between CPU and GPU if preprocessing is identical. Switch only if you benchmark antelopev2 on your photos and see a real gain that is worth the extra ~80 MB and the slower R100.

**AdaFace** ([paper](https://arxiv.org/abs/2204.00964), [repo](https://github.com/mk-minchul/AdaFace), CVPR 2022). A quality-adaptive margin loss; feature L2 norm is the quality proxy. Official README TAR @ FAR = 0.01% (1e-4):

| Model | Train set | IJB-C TAR @ FAR=1e-4 |
| --- | --- | --- |
| AdaFace R100 | MS1MV2 | 96.89 |
| AdaFace R50 | WebFace4M | 96.98 |
| AdaFace R100 | WebFace4M | 97.39 |
| AdaFace R100 | WebFace12M | **97.66** |
| buffalo_l (official zoo, IJB-C E4) | WebFace600K R50 | **97.25** |

AdaFace R100 @ WebFace12M is slightly above the published `buffalo_l` IJB-C figure. Gains are more interesting on *low-quality* protocols (IJB-S, TinyFace), which is the paper's focus. Caveats: it is not a drop-in InsightFace pack (different weights, you must match alignment and normalization); the headline models are mostly R100 (heavier on CPU than buffalo_l's R50); IJB numbers are not a guaranteed win on a small high-quality personal gallery; you would re-enroll and re-tune `T`. An AdaFace R50 is closer in cost but the official WebFace4M R50 IJB-C number (96.98) is not clearly ahead of `buffalo_l`.

**CPU conclusion.** For a consented personal photo app on CPU, `buffalo_l` remains the right default: official pack, 326 MB, strong IJB-C / CFP-FP numbers, and not clearly outclassed by antelopev2. Consider AdaFace (or a commercial InsightFace model) only if low-res or messy captures are a measured failure mode.

## 8. Licenses (accuracy-adjacent, but easy to miss)

- InsightFace **Python code** is MIT.
- **Pretrained packs** including `buffalo_l`, `buffalo_m`, `buffalo_s`, and `antelopev2` are "**available for non-commercial research purposes only**, including both automatically and manually downloaded models" ([model zoo](https://github.com/deepinsight/insightface/blob/master/model_zoo/README.md), [Python model zoo](https://github.com/deepinsight/insightface/blob/master/python-package/docs/model_zoo.md)). Commercial use needs a separate license from InsightFace ([licensing](https://www.insightface.ai/solutions/face-recognition-licensing), [contact](https://www.insightface.ai/contact)).
- AdaFace **code** is MIT ([LICENSE](https://github.com/mk-minchul/AdaFace/blob/master/LICENSE)). Pretrained-weight and training-set terms (MS1M / WebFace) still need a separate check before any commercial use.

A personal, non-commercial tool for Altair is a better fit for these packs than a public or paid service. If the project becomes a product, budget time to swap in licensed or own-trained weights and re-enroll.

## 9. Implementation checklist

1. Official InsightFace preprocess only: SCRFD → 5-point align → 112×112 → `normed_embedding`.
2. Filter enrollment (and, more loosely, probes) with `det_score`, face size, landmarks, and near-duplicate checks. Start from SDK defaults (score ≥ 0.6, registration min 32 px) and tighten size if you see bad labels.
3. Enroll 3+ varied photos per person; store all accepted embeddings; score with max similarity (plus optional flip-average TTA at enroll and probe).
4. Open-set rule: threshold `T` + top-1/top-2 margin + unknown/ambiguous outputs. Start `T` at **0.45**, calibrate on a frozen validation set, never on the test set.
5. Stay on `buffalo_l` until a head-to-head test on Altair's photos says otherwise.
6. Re-embed the gallery and re-tune `T` after any model, detection-size, or TTA change.

## Sources

- InsightFace model zoo: <https://github.com/deepinsight/insightface/blob/master/model_zoo/README.md>
- Python pack list, licenses, and accuracy table: <https://github.com/deepinsight/insightface/blob/master/python-package/docs/model_zoo.md>
- Model-zoo release assets: <https://github.com/deepinsight/insightface/releases/tag/model-zoo>
- Choosing a model, 1:1 vs 1:N, and threshold selection: <https://www.insightface.ai/guides/choose-face-recognition-model-and-evaluate>
- PersonAnalysis defaults (multi-sample enrollment, 0.45 similarity, 0.6 det score, 20/32 px mins): <https://github.com/deepinsight/insightface/blob/master/python-package/docs/person_analysis.md>
- Official IJB eval (flip TTA, template aggregation, 112×112 alignment): <https://github.com/deepinsight/insightface/blob/master/recognition/arcface_torch/eval_ijbc.py>
- buffalo_l vs Glint360K R100 discussion: <https://github.com/deepinsight/insightface/issues/1820>
- AdaFace paper: <https://arxiv.org/abs/2204.00964> and CVPR PDF <https://openaccess.thecvf.com/content/CVPR2022/papers/Kim_AdaFace_Quality_Adaptive_Margin_for_Face_Recognition_CVPR_2022_paper.pdf>
- AdaFace official results and code: <https://github.com/mk-minchul/AdaFace>
- InsightFace commercial licensing: <https://www.insightface.ai/solutions/face-recognition-licensing>
