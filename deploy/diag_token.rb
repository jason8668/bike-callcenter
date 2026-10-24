# Diagnostic for the Zammad 7.x Token model (run once, then delete).
# Prints ONLY structure, never secret values: column names, and for one
# freshly created-then-deleted test token, which attributes are set.
# Run inside the railsserver container:
#   bundle exec rails runner /tmp/diag_token.rb
puts "cols=#{Token.column_names.join(',')}"
admin = User.all.reject { |u| u.id == 1 }.find do |u|
  u.roles.any? { |r| r.permissions.any? { |p| p.name == 'admin' } }
end
admin ||= User.find_by(id: 2) || User.last
begin
  t = Token.create!(user_id: admin.id, persistent: true, action: 'api',
                    preferences: { 'permission' => ['admin', 'ticket.agent'] })
  filled = t.attributes.select { |_k, v| !v.nil? }.keys
  name_info = Token.column_names.include?('name') ? "name_nil=#{t.name.nil?}" : 'no name col'
  puts "created id=#{t.id} #{name_info} filled_keys=#{filled.join(',')}"
  begin
    t.destroy
  rescue StandardError
    Token.where(id: t.id).delete_all
  end
  puts 'test token removed'
rescue StandardError => e
  puts "create failed: #{e.class}: #{e.message.to_s[0, 200]}"
end
