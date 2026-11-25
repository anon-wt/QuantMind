import React from 'react';
import type { GPMiningOptions } from '../types-v2';

const FIELDS: Array<[keyof GPMiningOptions, string, number, number, number]> = [
  ['population_size', '种群规模', 8, 128, 1], ['generations', '演化代数', 1, 30, 1],
  ['top_k', '保留因子数', 1, 16, 1], ['max_depth', '公式深度', 2, 6, 1],
  ['crossover_rate', '交叉概率', 0, 1, 0.05], ['mutation_rate', '变异概率', 0, 1, 0.05],
  ['complexity_penalty', '复杂度惩罚', 0, 0.01, 0.0001], ['seed', '随机种子', 0, 2147483647, 1],
  ['min_days', '最少有效 IC 日', 5, 1000, 1],
];

export function GPOptionsPanel({ value, onChange, disabled = false }: { value: GPMiningOptions; onChange: (value: GPMiningOptions) => void; disabled?: boolean }) {
  return <section aria-label="GP 搜索参数" className="border-t border-slate-100 px-5 py-4 sm:px-6">
    <p className="mb-3 text-xs font-semibold">GP 遗传编程参数</p>
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">{FIELDS.map(([key, label, min, max, step]) => <label key={key} className="text-xs text-slate-500">{label}
      <input aria-label={`GP ${label}`} type="number" min={min} max={max} step={step} value={Number(value[key])} disabled={disabled} onChange={(event) => onChange({ ...value, [key]: Number(event.target.value) })} className="mt-1 w-full rounded border border-slate-200 px-2 py-1.5 text-slate-800" />
    </label>)}</div>
    <p className="mt-3 text-xs text-slate-500">种群规模 × 代数上限 2048。仅使用历史量价字段和白名单算子；样本外数据不参与搜索选优。</p>
    {value.population_size * value.generations > 2048 && <p role="alert" className="text-xs text-red-600">种群规模 × 代数不能超过 2048</p>}
  </section>;
}
