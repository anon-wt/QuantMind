import React from 'react';
import type { StatisticalMiningResult } from '../services-v2/taskHistoryApi';

const number = (value: number | null | undefined) => value == null ? '—' : value.toFixed(4);

export function GPResult({ result }: { result: StatisticalMiningResult }) {
  return <section className="space-y-3" aria-label="GP 遗传编程结果">
    <h4 className="font-medium">GP 遗传编程结果 · {result.selected.length} 个入选因子</h4>
    <p className="text-xs text-muted-foreground">预测周期 {result.prediction_horizon ?? result.label_spec?.horizon ?? 1} 个交易日，从当日收盘计。</p>
    <p className="text-xs text-amber-700">{result.gp_search?.warning}</p>
    <p className="text-xs text-muted-foreground">实际评估 {result.gp_search?.evaluated_formulas ?? 0} 个不同公式；样本外数据未参与遗传搜索与排序。</p>
    <div className="max-h-60 overflow-auto"><table className="w-full text-xs"><thead><tr><th>代数</th><th>累计公式数</th><th>最佳适应度</th><th>最佳 RankIC</th></tr></thead>
      <tbody>{result.gp_search?.history.map((row) => <tr key={row.generation} className="border-t"><td className="p-2">{row.generation}</td><td>{row.evaluated}</td><td>{number(row.best_fitness)}</td><td>{number(row.best_rank_ic)}</td></tr>)}</tbody></table></div>
    {result.candidates.map((item) => <article key={item.key} className="rounded border p-3 text-xs space-y-1">
      <p className="font-medium">{item.name} · RankIC {number(item.rank_ic_mean)} · ICIR {number(item.icir)} · 适应度 {number(item.fitness)}</p>
      <code className="block break-all">{item.expression}</code>
      <p className="text-muted-foreground">公式深度 {item.complexity?.depth ?? '—'} · 算子 {item.complexity?.op_count ?? '—'} · OOS RankIC {number(item.oos?.rank_ic_mean)}{item.oos?.reason ? `（${item.oos.reason}）` : ''}</p>
    </article>)}
  </section>;
}
