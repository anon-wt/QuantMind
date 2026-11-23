import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Checkbox, Space, Spin, Typography, message } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { dataPlatformService } from '../../services/dataPlatformService';
import { describeError } from '../quantdb/utils';

type MarketKey = 'quantdb' | 'quantus' | 'quanthk' | 'quantbc' | 'quantfutures';
type Source = { source: string; label: string; enabled: boolean };

interface MarketDataSourcesProps {
    market: MarketKey;
    marketLabel: string;
}

/** 与各市场数据源接口共用持久化配置，逐项保存避免覆盖其他选择。 */
export function MarketDataSources({ market, marketLabel }: MarketDataSourcesProps) {
    const [sources, setSources] = useState<Source[]>([]);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const loadSources = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const result = await dataPlatformService.getMarketDataSources(market);
            setSources(result.sources);
        } catch (err: unknown) {
            setError(describeError(err));
        } finally {
            setLoading(false);
        }
    }, [market]);

    useEffect(() => { void loadSources(); }, [loadSources]);

    const saveSource = async (source: string, enabled: boolean) => {
        setSaving(true);
        try {
            const result = await dataPlatformService.saveMarketDataSources(market, { [source]: enabled });
            setSources((current) => current.map((item) => ({
                ...item,
                enabled: result.sources[item.source] ?? item.enabled,
            })));
            message.success(`${marketLabel}数据源配置已保存`);
        } catch (err: unknown) {
            message.error(`保存数据源配置失败：${describeError(err)}`);
        } finally {
            setSaving(false);
        }
    };

    return (
        <Space direction="vertical" className="w-full" size="small">
            <Typography.Text strong>数据源配置</Typography.Text>
            {error ? (
                <Alert
                    type="error"
                    showIcon
                    message="加载数据源配置失败"
                    description={error}
                    action={<Button size="small" icon={<ReloadOutlined />} onClick={() => void loadSources()}>重试</Button>}
                />
            ) : (
                <Spin spinning={loading}>
                    <Space wrap>
                        {sources.map((item) => (
                            <Checkbox
                                key={item.source}
                                checked={item.enabled}
                                disabled={loading || saving}
                                onChange={(event) => void saveSource(item.source, event.target.checked)}
                            >
                                {item.label}
                            </Checkbox>
                        ))}
                    </Space>
                </Spin>
            )}
            <Typography.Text type="secondary" className="text-xs">
                选择需要启用的数据源，配置自动保存。
            </Typography.Text>
        </Space>
    );
}
