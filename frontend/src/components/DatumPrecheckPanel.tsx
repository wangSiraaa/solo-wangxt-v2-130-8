import { useState } from 'react';
import { api, type DatumPrecheck } from '../lib/api';

interface Props {
  projectId: number;
  onConfirmed?: () => void;
}

const verdictLabels: Record<DatumPrecheck['verdict'], string> = {
  datumless_can_add: '可补基准',
  compatible: '相容可确认',
  conflict: '基准矛盾风险',
  existing_inconsistency: '既有基准已冲突',
  indeterminate: '信息不足'
};

function formatMeters(value: number | undefined): string {
  return value === undefined ? '—' : `${value.toFixed(4)} m`;
}

export function DatumPrecheckPanel({ projectId, onConfirmed }: Props) {
  const [pointCode, setPointCode] = useState('');
  const [elevation, setElevation] = useState('');
  const [sigma, setSigma] = useState('0.001');
  const [precheck, setPrecheck] = useState<DatumPrecheck | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [confirmed, setConfirmed] = useState(false);

  const dangerous = precheck?.risk_level === 'danger';

  async function runPrecheck() {
    if (!pointCode.trim() || elevation === '') {
      setError('请填写测点编号与高程。');
      return;
    }
    setBusy(true);
    setError('');
    setConfirmed(false);
    setPrecheck(null);
    try {
      const result = await api<DatumPrecheck>(`/api/projects/${projectId}/datums/precheck`, {
        method: 'POST',
        body: JSON.stringify({
          point_code: pointCode.trim(),
          elevation_m: Number(elevation),
          sigma_m: Number(sigma)
        })
      });
      setPrecheck(result);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function confirmDatum() {
    if (!precheck) return;
    setBusy(true);
    setError('');
    try {
      // Confirmation goes through the normal optimistic revision API. The
      // precheck is never treated as a solve and cannot bypass a version bump.
      const result = await api<{ id: number; draft_lock_version: number }>(
        `/api/projects/${projectId}/datums`,
        {
          method: 'POST',
          body: JSON.stringify({
            point_code: precheck.candidate.point_code,
            elevation_m: precheck.candidate.elevation_m,
            sigma_m: precheck.candidate.sigma_m,
            lock_version: precheck.draft_lock_version
          })
        }
      );
      setConfirmed(true);
      setPrecheck(null);
      setError(`基准已保存（id=${result.id}，草稿版本 v${result.draft_lock_version}）。`);
      onConfirmed?.();
    } catch (err) {
      setError((err as Error).message);
      setPrecheck({ ...precheck });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="datum-precheck">
      <div className="precheck-form">
        <label>
          测点编号
          <input value={pointCode} onChange={(e) => setPointCode(e.target.value)} placeholder="例如 BM-A" />
        </label>
        <label>
          高程 (m)
          <input
            value={elevation}
            onChange={(e) => setElevation(e.target.value)}
            type="number"
            step="0.0001"
            placeholder="100.0000"
          />
        </label>
        <label>
          精度 σ (m)
          <input value={sigma} onChange={(e) => setSigma(e.target.value)} type="number" step="0.0001" />
        </label>
        <button onClick={runPrecheck} disabled={busy}>
          预检（只读）
        </button>
      </div>

      {error && <div className="precheck-error">{error}</div>}
      {confirmed && !error && <div className="precheck-ok">基准已确认写入。</div>}

      {precheck && (
        <div className={`precheck-result risk-${precheck.risk_level}`}>
          <div className="precheck-head">
            <span className={`risk-badge risk-${precheck.risk_level}`}>
              {verdictLabels[precheck.verdict]}
            </span>
            <span className="precheck-version">
              草稿版本 v{precheck.draft_lock_version} · 分量 #{precheck.component.index} ·{' '}
              {precheck.component.point_count} 点 / {precheck.component.observation_count} 测段
            </span>
          </div>

          <p className="precheck-message">{precheck.message}</p>

          <div className="precheck-cols">
            <div>
              <h3>所属连通分量</h3>
              <ul className="fact-list">
                <li>分量编号：#{precheck.component.index}</li>
                <li>测点数：{precheck.component.point_count}</li>
                <li>测段数：{precheck.component.observation_count}</li>
                <li>现有基准数：{precheck.component.datum_count}</li>
                <li>样例测点：{precheck.component.sample_points.join('、')}</li>
              </ul>
            </div>
            <div>
              <h3>现有基准</h3>
              {precheck.existing_datums.length === 0 ? (
                <p className="muted">该分量当前没有基准（求解会 blocked_rank_deficient），候选点可补基准。</p>
              ) : (
                <table className="datum-table">
                  <thead>
                    <tr>
                      <th>测点</th>
                      <th>高程 (m)</th>
                      <th>σ (m)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {precheck.existing_datums.map((datum) => (
                      <tr key={`${datum.point_id}-${datum.id ?? ''}`}>
                        <td>{datum.point_code ?? datum.point_id}</td>
                        <td>{datum.elevation_m.toFixed(4)}</td>
                        <td>{datum.sigma_m.toFixed(4)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>

          {precheck.conflicts.length > 0 && (
            <div className="precheck-conflicts">
              <h3>潜在冲突（预检不保存任何数据）</h3>
              <ul>
                {precheck.conflicts.map((conflict, index) => (
                  <li key={index}>
                    {conflict.kind === 'same_point_datum' && (
                      <>
                        测点 <strong>{conflict.point_code}</strong> 已有基准 {formatMeters(conflict.existing_m)}
                        ，候选 {formatMeters(conflict.declared_m)}，偏差 {formatMeters(conflict.residual_m)}
                        ，阈值 {formatMeters(conflict.threshold_m)}。
                      </>
                    )}
                    {conflict.kind === 'network_implied_elevation' && (
                      <>
                        既有基准网经测线传播隐含 <strong>{conflict.point_code}</strong> 高程{' '}
                        {formatMeters(conflict.implied_m)}，候选 {formatMeters(conflict.declared_m)}，
                        偏差 {formatMeters(conflict.residual_m)}，超过 3σ 阈值{' '}
                        {formatMeters(conflict.threshold_m)}；正式求解将 blocked_datum_contradiction。
                      </>
                    )}
                    {conflict.kind === 'existing_inconsistency' && (
                      <>
                        该分量已有基准互相矛盾（{conflict.status}），请先解决既有冲突，不能叠加新基准。
                      </>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="precheck-actions">
            <button className="primary" onClick={confirmDatum} disabled={busy || dangerous}>
              按乐观锁确认写入
            </button>
            {dangerous && <span className="muted">存在明显矛盾，已阻止直接确认；预检未保存。</span>}
          </div>
        </div>
      )}
    </div>
  );
}
