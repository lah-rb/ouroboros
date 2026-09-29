//! OLMo 2 over quantized weights: candle-transformers' `olmo2` module with every linear
//! layer a `QMatMul` over GGUF tensors. Tensor names are the Hugging Face ones, since
//! `spectra-demo quantize` writes the checkpoint's own names. Activations stay f32.
//!
//! OLMo 2 differs from Llama in two ways that matter here: RMS norms on the full q and k
//! projections (before the head split), and norms AFTER attention and after the MLP
//! instead of before them.

use candle_core::{DType, Device, Module, Result, Tensor, D};
use candle_transformers::quantized_nn::{linear_no_bias, Embedding, Linear, RmsNorm};
use candle_transformers::quantized_var_builder::VarBuilder;
use std::sync::Arc;

#[derive(Debug, Clone, serde::Deserialize)]
pub struct Config {
    pub vocab_size: usize,
    pub hidden_size: usize,
    pub intermediate_size: usize,
    pub num_hidden_layers: usize,
    pub num_attention_heads: usize,
    pub num_key_value_heads: usize,
    pub rms_norm_eps: f64,
    pub max_position_embeddings: usize,
    pub rope_theta: f64,
    pub tie_word_embeddings: bool,
}

impl Config {
    /// From a Hugging Face config.json. transformers 5 moved `rope_theta` into
    /// `rope_parameters`; the base checkpoint still has it at the top level.
    pub fn from_hf_json(text: &str) -> anyhow::Result<Self> {
        let mut v: serde_json::Value = serde_json::from_str(text)?;
        if v.get("rope_theta").is_none() {
            let theta = v["rope_parameters"]["rope_theta"].clone();
            anyhow::ensure!(!theta.is_null(), "config has no rope_theta");
            v["rope_theta"] = theta;
        }
        Ok(serde_json::from_value(v)?)
    }
}

struct Rotary {
    sin: Tensor,
    cos: Tensor,
}

impl Rotary {
    fn new(cfg: &Config, dev: &Device) -> Result<Self> {
        let dim = cfg.hidden_size / cfg.num_attention_heads;
        let inv: Vec<f32> = (0..dim)
            .step_by(2)
            .map(|i| 1f32 / cfg.rope_theta.powf(i as f64 / dim as f64) as f32)
            .collect();
        let n = inv.len();
        let inv = Tensor::from_vec(inv, (1, n), dev)?;
        let t = Tensor::arange(0u32, cfg.max_position_embeddings as u32, dev)?
            .to_dtype(DType::F32)?
            .reshape((cfg.max_position_embeddings, 1))?;
        let freqs = t.matmul(&inv)?;
        Ok(Self { sin: freqs.sin()?, cos: freqs.cos()? })
    }

    fn apply(&self, q: &Tensor, k: &Tensor, offset: usize) -> Result<(Tensor, Tensor)> {
        let (_, _, seq, _) = q.dims4()?;
        let cos = self.cos.narrow(0, offset, seq)?;
        let sin = self.sin.narrow(0, offset, seq)?;
        Ok((
            candle_nn::rotary_emb::rope(&q.contiguous()?, &cos, &sin)?,
            candle_nn::rotary_emb::rope(&k.contiguous()?, &cos, &sin)?,
        ))
    }
}

struct Attention {
    q_proj: Linear,
    k_proj: Linear,
    v_proj: Linear,
    o_proj: Linear,
    q_norm: RmsNorm,
    k_norm: RmsNorm,
    n_heads: usize,
    n_kv: usize,
    head_dim: usize,
    hidden: usize,
    rotary: Arc<Rotary>,
    cache: Option<(Tensor, Tensor)>,
}

impl Attention {
    fn new(rotary: Arc<Rotary>, cfg: &Config, vb: VarBuilder) -> Result<Self> {
        let h = cfg.hidden_size;
        let hd = h / cfg.num_attention_heads;
        let kv = cfg.num_key_value_heads * hd;
        Ok(Self {
            q_proj: linear_no_bias(h, h, vb.pp("q_proj"))?,
            k_proj: linear_no_bias(h, kv, vb.pp("k_proj"))?,
            v_proj: linear_no_bias(h, kv, vb.pp("v_proj"))?,
            o_proj: linear_no_bias(h, h, vb.pp("o_proj"))?,
            q_norm: RmsNorm::new(h, cfg.rms_norm_eps, vb.pp("q_norm"))?,
            k_norm: RmsNorm::new(kv, cfg.rms_norm_eps, vb.pp("k_norm"))?,
            n_heads: cfg.num_attention_heads,
            n_kv: cfg.num_key_value_heads,
            head_dim: hd,
            hidden: h,
            rotary,
            cache: None,
        })
    }

    fn forward(&mut self, xs: &Tensor, mask: Option<&Tensor>, offset: usize) -> Result<Tensor> {
        let (b, n, _) = xs.dims3()?;
        let q = self.q_norm.forward(&self.q_proj.forward(xs)?)?;
        let k = self.k_norm.forward(&self.k_proj.forward(xs)?)?;
        let v = self.v_proj.forward(xs)?;
        let q = q.reshape((b, n, self.n_heads, self.head_dim))?.transpose(1, 2)?;
        let k = k.reshape((b, n, self.n_kv, self.head_dim))?.transpose(1, 2)?;
        let v = v.reshape((b, n, self.n_kv, self.head_dim))?.transpose(1, 2)?.contiguous()?;
        let (q, k) = self.rotary.apply(&q, &k, offset)?;
        let (k, v) = match &self.cache {
            None => (k, v),
            Some((pk, pv)) => (Tensor::cat(&[pk, &k], 2)?, Tensor::cat(&[pv, &v], 2)?),
        };
        self.cache = Some((k.clone(), v.clone()));
        let groups = self.n_heads / self.n_kv;
        let k = candle_transformers::utils::repeat_kv(k, groups)?.contiguous()?;
        let v = candle_transformers::utils::repeat_kv(v, groups)?.contiguous()?;
        let scale = 1f64 / (self.head_dim as f64).sqrt();
        let w = (q.matmul(&k.transpose(2, 3)?)? * scale)?;
        let w = match mask {
            None => w,
            Some(m) => w.broadcast_add(m)?,
        };
        let w = candle_nn::ops::softmax_last_dim(&w)?;
        w.matmul(&v)?
            .transpose(1, 2)?
            .reshape((b, n, self.hidden))?
            .apply(&self.o_proj)
    }
}

struct Mlp {
    gate: Linear,
    up: Linear,
    down: Linear,
}

impl Mlp {
    fn new(cfg: &Config, vb: VarBuilder) -> Result<Self> {
        let (h, i) = (cfg.hidden_size, cfg.intermediate_size);
        Ok(Self {
            gate: linear_no_bias(h, i, vb.pp("gate_proj"))?,
            up: linear_no_bias(h, i, vb.pp("up_proj"))?,
            down: linear_no_bias(i, h, vb.pp("down_proj"))?,
        })
    }

    fn forward(&self, xs: &Tensor) -> Result<Tensor> {
        let g = candle_nn::ops::silu(&self.gate.forward(xs)?)?;
        (g * self.up.forward(xs)?)?.apply(&self.down)
    }
}

struct Layer {
    attn: Attention,
    mlp: Mlp,
    post_attn: RmsNorm,
    post_ff: RmsNorm,
}

pub struct Model {
    embed: Embedding,
    layers: Vec<Layer>,
    norm: RmsNorm,
    lm_head: Linear,
    device: Device,
}

impl Model {
    pub fn new(cfg: &Config, vb: VarBuilder) -> Result<Self> {
        let dev = vb.device().clone();
        let vm = vb.pp("model");
        let rotary = Arc::new(Rotary::new(cfg, &dev)?);
        let mut layers = Vec::with_capacity(cfg.num_hidden_layers);
        for i in 0..cfg.num_hidden_layers {
            let vl = vm.pp("layers").pp(i);
            layers.push(Layer {
                attn: Attention::new(rotary.clone(), cfg, vl.pp("self_attn"))?,
                mlp: Mlp::new(cfg, vl.pp("mlp"))?,
                post_attn: RmsNorm::new(cfg.hidden_size, cfg.rms_norm_eps, vl.pp("post_attention_layernorm"))?,
                post_ff: RmsNorm::new(cfg.hidden_size, cfg.rms_norm_eps, vl.pp("post_feedforward_layernorm"))?,
            });
        }
        let lm_head = if cfg.tie_word_embeddings {
            linear_no_bias(cfg.hidden_size, cfg.vocab_size, vm.pp("embed_tokens"))?
        } else {
            linear_no_bias(cfg.hidden_size, cfg.vocab_size, vb.pp("lm_head"))?
        };
        Ok(Self {
            embed: Embedding::new(cfg.vocab_size, cfg.hidden_size, vm.pp("embed_tokens"))?,
            layers,
            norm: RmsNorm::new(cfg.hidden_size, cfg.rms_norm_eps, vm.pp("norm"))?,
            lm_head,
            device: dev,
        })
    }

    /// Logits for the last position, shape (vocab,). `offset` = tokens already cached.
    pub fn forward(&mut self, ids: &[u32], offset: usize) -> Result<Tensor> {
        let n = ids.len();
        let input = Tensor::new(ids, &self.device)?.unsqueeze(0)?;
        let mask = if n > 1 {
            let m: Vec<f32> = (0..n)
                .flat_map(|i| (0..n + offset).map(move |j| if j > i + offset { f32::NEG_INFINITY } else { 0. }))
                .collect();
            Some(Tensor::from_slice(&m, (1, 1, n, n + offset), &self.device)?)
        } else {
            None
        };
        let mut xs = self.embed.forward(&input)?;
        for l in self.layers.iter_mut() {
            let r = xs.clone();
            let a = l.post_attn.forward(&l.attn.forward(&xs, mask.as_ref(), offset)?)?;
            let xs1 = (a + r)?;
            let f = l.post_ff.forward(&l.mlp.forward(&xs1)?)?;
            xs = (xs1 + f)?;
        }
        xs.narrow(1, n - 1, 1)?
            .apply(&self.norm)?
            .apply(&self.lm_head)?
            .squeeze(0)?
            .squeeze(0)?
            .to_dtype(DType::F32)
    }

    pub fn clear_cache(&mut self) {
        for l in self.layers.iter_mut() {
            l.attn.cache = None;
        }
    }
}

/// Argmax over the logits (greedy decoding, as the probes ran).
pub fn argmax(logits: &Tensor) -> Result<u32> {
    logits.argmax(D::Minus1)?.to_scalar::<u32>()
}
