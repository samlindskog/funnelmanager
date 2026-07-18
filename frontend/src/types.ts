export interface User {
  username: string
  role: string
}

export interface AppLink {
  name: string
  description: string
  url: string
}

export interface Grant {
  service: string
  methods: string[]
  path_prefix: string
}

export interface Role {
  name: string
  description: string
  grants: Grant[]
  created_at?: string | null
}

export interface ChannelLink {
  channel: string
  device_id: string
  username: string
  display_name?: string
  linked_at?: string
}

export interface UserDetail extends User {
  created_at?: string | null
  channels: ChannelLink[]
}

export interface AccountRequest {
  username: string
  requested_at?: string
  last_seen_at?: string
}

export interface ChannelRequest {
  channel: string
  device_id: string
  display_name?: string
  requested_at?: string
  last_seen_at?: string
}
