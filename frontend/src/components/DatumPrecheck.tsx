import { useState } from 'react';
import { api, type DatumAssessment, type DatumPrecheckResponse } from '../lib/api';

interface Props {
  projectId: number;
}

const assessmentText: Record<DatumAssessment, string> = {
  fills_datum_gap: '可补基准：该连通分量当前没有基准，新增候选可补齐垂直基准',
  consistent: '与既有基准一致：候选高程在 3σ 容差内',
  contradiction_risk: '风险：候选高程与既有基准明显矛盾，预检不会保存，请核实后重新预检'
};

export function DatumPrecheck({ projectId }: Props) {
  const [pointCode, setPointCode] = useState('');
  const [elevation, setElevation] = useState('');
  const [sigma, setSigma] = useState('0.001');
  const [report, setReport] = useState<DatumPrecheckResponse | null>(null);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);

  async function runPrecheck() {
    setBusy(true);
    setMessage('');
    try {
      const result = await api<DatumPrecheckResponse>(`/api/projects/${projectId}/datum-precheck`, {
        method: 'POST',
        body: JSON.stringify({
          point_code: pointCode.trim(),
          elevation_m: Number(elevation),
          sigma_m: Number(sigma)
        })
      });
      setReport(result);
    } catch (error) {
      setReport(null);
      setMessage((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function confirmDatum() {
    if (!report) return;
    setBusy(true);
    try {
      // 确认仍走乐观锁修订 API：携带预检时的草稿版本，过期即 409。
      const result = await api<{ id: number; draft_lock_version: number }>(`/api/projects/${projectId}/datums`, {
        method: 'POST',
        body: JSON.stringify({
          point_code: report.candidate.point_code,
          elevation_m: report.candidate.elevation_m,
          sigma_m: report.candidate.sigma_m,
          lock_version: report.draft_lock_version
        })
      });
      setMessage(`基准已保存（草稿版本 v${result.draft_lock_version}）。预检仅作筛查，正式成果以整体求解为准。`);
      setReport(null);
    } catch (error) {
      const text = (error as Error).message;
      if (text.startsWith('409')) {
        setMessage('草稿已被他人修改（409 版本冲突）：预检结果已过期，请重新预检。');
        setReport(null);
      } else {
        setMessage(text);
      }
    } finally {
      setBusy(false);
    }
  }

  const blocked = report?.assessment === 'contradiction_risk';

  return (
    <div className="datum-precheck">
      <div className="precheck-form">
        <label>
          拟新增测点
          <input value={pointCode} onChange={(event) => setPointCode(event.target.value)} placeholder="如 BM-C" />
        </label>
        <label>
          高程 (m)
          <input value={elevation} onChange={(event) => setElevation(event.target.value)} type="number" step="0.0001" />
        </label>
        <label>
          精度 σ (m)
          <input value={sigma} onChange={(event) => setSigma(event.target.value)} type="number" step="0.0001" />
        </label>
        <button onClick={runPrecheck} disabled={busy || !pointCode.trim() || !elevation}>
          预检
        </button>
        <button
          onClick={confirmDatum}
          disabled={busy || !report || blocked}
          className="primary"
          title={blocked ? '存在基准矛盾风险，不能保存' : undefined}
        >
          确认新增基准
        </button>
      </div>

      {message && <div className="message">{message}</div>}

      {report && (
        <div className={`precheck-report precheck-${report.assessment}`}>
          <p className="precheck-assessment">{assessmentText[report.assessment]}</p>
          <dl className="facts">
            <dt>所属连通分量</dt>
            <dd>
              #{report.component.index}（{report.component.point_count} 点 / {report.component.observation_count} 测段 /{' '}
              {report.component.datum_count} 基准{report.component.isolated ? '，孤立点' : ''}）
            </dd>
            <dt>候选</dt>
            <dd>
              {report.candidate.point_code} = {report.candidate.elevation_m.toFixed(4)} m ± {report.candidate.sigma_m} m
            </dd>
            <dt>草稿版本</dt>
            <dd>v{report.draft_lock_version}（确认时随乐观锁提交）</dd>
          </dl>

          {report.existing_datums.length > 0 && (
            <table className="residual-table">
              <thead>
                <tr>
                  <th>现有基准</th>
                  <th>声明高程 (m)</th>
                  <th>推算候选高程 (m)</th>
                  <th>差值 (mm)</th>
                  <th>3σ 容差 (mm)</th>
                  <th>结论</th>
                </tr>
              </thead>
              <tbody>
                {report.existing_datums.map((check) => (
                  <tr key={check.datum_id} className={check.within_tolerance ? '' : 'residual-bad'}>
                    <td>{check.point_code ?? check.point_id}</td>
                    <td>{check.declared_elevation_m.toFixed(4)}</td>
                    <td>{check.implied_candidate_elevation_m.toFixed(4)}</td>
                    <td>{(check.discrepancy_m * 1000).toFixed(2)}</td>
                    <td>{(check.tolerance_m * 1000).toFixed(2)}</td>
                    <td>{check.within_tolerance ? '一致' : '矛盾'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {report.conflicts.length > 0 && (
            <p className="precheck-conflict">
              潜在冲突：与 {report.conflicts.map((c) => c.point_code ?? c.point_id).join('、')} 矛盾，未保存任何数据。
            </p>
          )}
          <small>{report.notice}</small>
        </div>
      )}
    </div>
  );
}
