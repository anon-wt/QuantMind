import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Input, Space, Tag, Typography, message } from 'antd';
import { ReloadOutlined, SaveOutlined } from '@ant-design/icons';
import { dataPlatformService, DatayesConfig } from '../../services/dataPlatformService';
import { describeError } from '../quantdb/utils';
import { SourceDatasetCatalog } from './SourceDatasetCatalog';

export function DatayesConfigPanel() {
    const [config, setConfig] = useState<DatayesConfig | null>(null);
    const [token, setToken] = useState('');
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const loadConfig = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            setConfig(await dataPlatformService.getDatayesConfig());
        } catch (err: unknown) {
            setError(describeError(err));
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => { void loadConfig(); }, [loadConfig]);

    const saveConfig = async () => {
        const value = token.trim();
        if (!value || /\s/.test(value) || /[\r\n]/.test(token) || value.length > 4096) {
            message.warning('请输入有效的 Datayes Token');
            return;
        }
        setSaving(true);
        try {
            setConfig(await dataPlatformService.saveDatayesConfig(value));
            setToken('');
            message.success('Datayes Token 已保存');
        } catch (err: unknown) {
            message.error(`保存 Datayes Token 失败：${describeError(err)}`);
        } finally {
            setSaving(false);
        }
    };

    return (
        <Space direction="vertical" className="w-full" size="small">
            <Space wrap>
                <Typography.Text strong>Datayes 数据商城 Token</Typography.Text>
                {config && (
                    <Tag color={config.token_configured ? 'green' : 'default'}>
                        {config.token_configured ? `已配置 ${config.token_masked}` : '未配置'}
                    </Tag>
                )}
            </Space>
            {error && (
                <Alert
                    type="error"
                    showIcon
                    message="加载 Datayes 配置失败"
                    description={error}
                    action={<Button size="small" icon={<ReloadOutlined />} onClick={() => void loadConfig()}>重试</Button>}
                />
            )}
            <Space wrap>
                <Input.Password
                    aria-label="Datayes Token"
                    placeholder={config?.token_configured ? '输入新 Token 以替换' : '粘贴 Datayes 数据商城 Token'}
                    value={token}
                    onChange={(event) => setToken(event.target.value)}
                    onPressEnter={() => { if (!loading && !saving && !error) void saveConfig(); }}
                    disabled={loading || saving || Boolean(error)}
                    autoComplete="off"
                    style={{ width: 320, maxWidth: '100%' }}
                />
                <Button
                    type="primary"
                    icon={<SaveOutlined />}
                    loading={saving}
                    disabled={loading || Boolean(error) || !token.trim()}
                    onClick={() => void saveConfig()}
                >
                    保存 Token
                </Button>
                <Typography.Link href="https://mall.datayes.com/?registSource=datamall" target="_blank" rel="noopener noreferrer">
                    打开 Datayes 数据商城
                </Typography.Link>
            </Space>
            <Typography.Text type="secondary" className="text-xs">
                Token 保存在服务端，重启后仍保留，页面仅显示掩码。保存后到管理后台 → 数据平台 → A 股，勾选 Datayes 并初始化数据。实际数据权限以数据商城订阅为准。
            </Typography.Text>
            {config?.datasets && <SourceDatasetCatalog datasets={config.datasets} />}
        </Space>
    );
}
