"""Does muse actually do grounded detection? Ask it three ways and look."""
import json, time, urllib.request
IMG = "/Users/lah-rb/ouroboros-runs/vl_bakeoff_20260811/figures/mineralogy.png"
PROMPTS = {
 "native": "Detect all objects in this image. Return the result in your native object-detection format.",
 "explicit_json": ("Locate every bar in the stacked bar chart on the left. Return ONLY JSON: "
   '[{"label": "<x-axis sample name>", "box_2d": [x1, y1, x2, y2]}] with pixel coordinates. No prose.'),
 "point": "Point to the Iceland-M bar in this image. Give its location as coordinates.",
}
for name, q in PROMPTS.items():
    payload = json.dumps({"messages":[{"role":"user","content":[
        {"type":"text","text":q},{"type":"image_path","path":IMG}]}],
        "max_tokens":700,"temperature":0.2}).encode()
    r = urllib.request.Request("http://localhost:8008/v1/vision", data=payload,
                               headers={"Content-Type":"application/json"})
    t=time.time()
    try:
        with urllib.request.urlopen(r, timeout=900) as resp: d=json.loads(resp.read())
        txt=(d["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        txt=f"ERROR {e}"
    print("="*72); print(f"PROMPT: {name}  ({time.time()-t:.0f}s)"); print("="*72)
    print(txt[:1400]); print()
