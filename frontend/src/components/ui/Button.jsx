export function Button({ variant = 'primary', icon: Icon, children, ...props }) {
  return (
    <button className={`btn btn-${variant}`} {...props}>
      {Icon && <Icon size={15} />}
      {children}
    </button>
  )
}
