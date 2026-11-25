import React from 'react';

export interface QualityConfig {
  min_is_days: number; min_oos_days: number; min_names: number; min_coverage: number;
  min_abs_rank_ic: number; min_abs_icir: number; min_abs_t: number;
  min_fold_consistency: number; min_oos_retention: number; min_oos_periods: number; fdr_q: number;
}
export const DEFAULT_QUALITY_CONFIG: QualityConfig = {
  min_is_days: 252, min_oos_days: 126, min_names: 30, min_coverage: 0.8,
  min_abs_rank_ic: 0.02, min_abs_icir: 0.3, min_abs_t: 3,
  min_fold_consistency: 0.75, min_oos_retention: 0.5, min_oos_periods: 20, fdr_q: 0.1,
};
const fields: Array<{key: keyof QualityConfig; label: string; min: number; max: number; step: number}> = [
  {key:'min_is_days', label:'样本内最少有效日', min:20, max:5000, step:1},
  {key:'min_oos_days', label:'样本外最少有效日', min:20, max:5000, step:1},
  {key:'min_names', label:'每日最少股票数', min:10, max:10000, step:1},
  {key:'min_coverage', label:'最低数值覆盖率', min:0.01, max:1, step:0.01},
  {key:'min_abs_rank_ic', label:'最低方向调整 RankIC', min:0.0001, max:1, step:0.001},
  {key:'min_abs_icir', label:'最低非年化 ICIR', min:0.001, max:100, step:0.01},
  {key:'min_abs_t', label:'样本外最低 HAC t 值', min:0.01, max:100, step:0.1},
  {key:'min_fold_consistency', label:'四段方向一致率', min:0.01, max:1, step:0.25},
  {key:'min_oos_retention', label:'样本外最低 IC 保留比例', min:0, max:2, step:0.05},
  {key:'min_oos_periods', label:'样本外最少不重叠持有期', min:5, max:1000, step:1},
  {key:'fdr_q', label:'BH-FDR 最大 q 值', min:0.001, max:0.2, step:0.01},
];
export const qualityError = (value: QualityConfig) => fields.some(f => !Number.isFinite(value[f.key]) || value[f.key] < f.min || value[f.key] > f.max || (f.step === 1 && !Number.isInteger(value[f.key]))) ? '请检查统一质量阈值的范围' : null;

export const QualitySummary: React.FC<{value: QualityConfig}> = ({value}) => <div className="mt-2 text-xs text-slate-600">
  样本内/外至少 {value.min_is_days}/{value.min_oos_days} 有效日，每日 ≥{value.min_names} 只股票；覆盖率 ≥{value.min_coverage * 100}%；RankIC ≥{value.min_abs_rank_ic}，非年化 ICIR ≥{value.min_abs_icir}；样本外 HAC t ≥{value.min_abs_t}，BH-FDR q ≤{value.fdr_q}；四段同向 ≥{value.min_fold_consistency * 100}%，样本外 IC 保留 ≥{value.min_oos_retention * 100}%，不重叠持有期 ≥{value.min_oos_periods} 个。
</div>;

export const QualityPanel: React.FC<{value: QualityConfig; disabled: boolean; onChange: (value: QualityConfig) => void}> = ({value, disabled, onChange}) => <details className="border-t border-slate-100 px-5 py-3 text-xs sm:px-6">
  <summary className="cursor-pointer font-semibold text-slate-700">统一因子质量标准（仅合格因子入库）</summary>
  <p className="mt-2 text-slate-500">三种模式使用相同检查。方向由样本内固定，样本外不得反向择优；缺失证据或样本不足均不入库。阈值随本次任务保存。</p>
  <fieldset disabled={disabled} className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3">{fields.map(f => <label key={f.key} className="space-y-1 text-slate-600">{f.label}<input aria-label={f.label} type="number" min={f.min} max={f.max} step={f.step} value={Number.isFinite(value[f.key]) ? value[f.key] : ''} onChange={e => onChange({...value, [f.key]: e.target.value === '' ? NaN : Number(e.target.value)})} className="w-full rounded border border-slate-200 px-2 py-1" /></label>)}</fieldset>
  {qualityError(value) && <p role="alert" className="mt-2 text-red-600">{qualityError(value)}</p>}
  <button type="button" disabled={disabled} onClick={() => onChange({...DEFAULT_QUALITY_CONFIG})} className="mt-3 rounded border px-2 py-1">恢复推荐阈值</button>
  <p className="mt-2 text-slate-400">推荐值是研究入库起点。该检查不替代扣费后组合回测；反复根据同一验证集调阈值会削弱样本外证据。</p>
</details>;

export interface QualityAudit {
  evaluated: number; accepted: number; rejected: number; config: QualityConfig;
  candidates: Record<string, {passed: boolean; reasons: string[]; direction?: number;
    oos?: {rank_ic_mean: number | null; icir: number | null; n_days: number; t_stat: number | null}}>;
}
export const QualityAuditView: React.FC<{audit?: QualityAudit | null}> = ({audit}) => !audit ? null : <section className="rounded-lg border border-slate-200 p-3 text-xs">
  <h4 className="font-semibold">统一质量检查：{audit.evaluated} 个候选，{audit.accepted} 个合格，{audit.rejected} 个未入库</h4>
  <QualitySummary value={audit.config} />
  <div className="mt-3 max-h-64 space-y-2 overflow-auto">{Object.entries(audit.candidates).map(([key, item]) => <article key={key} className="border-t pt-2"><p className="break-all font-medium">{key} · {item.passed ? '合格入库' : '未入库'}{item.direction === -1 ? ' · 反向使用' : ''}</p>{item.oos && <p>样本外有效日 {item.oos.n_days} · 原始 RankIC {item.oos.rank_ic_mean?.toFixed(4) ?? '—'} · ICIR {item.oos.icir?.toFixed(3) ?? '—'} · HAC t {item.oos.t_stat?.toFixed(2) ?? '—'}</p>}<p className="text-amber-700">{item.reasons.join('；')}</p></article>)}</div>
</section>;
