import React from 'react';
import { Database } from 'lucide-react';
import { DatayesConfigPanel } from '../../admin/components/data-management/DatayesConfigPanel';

export default function DatayesSettings() {
  return (
    <div className="w-full space-y-3">
      <div className="bg-white rounded-2xl border border-slate-200/80 px-5 py-3.5 shadow-xs flex items-center gap-3">
        <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-cyan-500 to-blue-600 flex items-center justify-center text-white shadow-sm">
          <Database className="w-4 h-4" />
        </div>
        <div>
          <h3 className="text-sm font-black text-slate-800 m-0">数据平台 (Datayes)</h3>
          <p className="text-[11px] text-slate-400 m-0 leading-tight">通联数据商城的 A 股行情、基础资料和财务数据访问凭据</p>
        </div>
      </div>
      <div className="bg-white rounded-2xl border border-slate-200/80 p-4 shadow-xs">
        <DatayesConfigPanel />
      </div>
    </div>
  );
}
