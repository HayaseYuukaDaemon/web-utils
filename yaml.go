package main

import (
	"fmt"

	"gopkg.in/yaml.v3"
)

type EditableYAML struct{ *yaml.Node }

func WrapYAML(node *yaml.Node) *EditableYAML {
	if node == nil {
		return nil
	}
	return &EditableYAML{Node: node}
}

func (m *EditableYAML) GetMapValue(key string) *EditableYAML {
	if m == nil || m.Node == nil || m.Kind != yaml.MappingNode {
		return nil
	}
	for i := 0; i+1 < len(m.Content); i += 2 {
		if m.Content[i].Value == key {
			return WrapYAML(m.Content[i+1])
		}
	}
	return nil
}

func (m *EditableYAML) SetMapValue(key string, value any) error {
	if m == nil || m.Node == nil || m.Kind != yaml.MappingNode {
		return fmt.Errorf("expected mapping node")
	}
	for i := 0; i+1 < len(m.Content); i += 2 {
		if m.Content[i].Value != key {
			continue
		}
		old := m.Content[i+1]
		var node yaml.Node
		if err := node.Encode(value); err != nil {
			return fmt.Errorf("encode value for key %q: %w", key, err)
		}
		node.HeadComment = old.HeadComment
		node.LineComment = old.LineComment
		node.FootComment = old.FootComment
		m.Content[i+1] = &node
		return nil
	}
	return fmt.Errorf("key %q not found", key)
}

func (m *EditableYAML) AddMapValue(key string, value any) error {
	if m == nil || m.Node == nil || m.Kind != yaml.MappingNode {
		return fmt.Errorf("expected mapping node")
	}
	if m.GetMapValue(key) != nil {
		return fmt.Errorf("key %q already exists", key)
	}
	var keyNode yaml.Node
	if err := keyNode.Encode(key); err != nil {
		return fmt.Errorf("encode key %q: %w", key, err)
	}
	var valueNode yaml.Node
	if err := valueNode.Encode(value); err != nil {
		return fmt.Errorf("encode value for key %q: %w", key, err)
	}
	m.Content = append(
		m.Content,
		&keyNode,
		&valueNode,
	)
	return nil
}

func (m *EditableYAML) DeleteMapValue(key string) bool {
	if m == nil || m.Node == nil || m.Kind != yaml.MappingNode {
		return false
	}
	for i := 0; i+1 < len(m.Content); i += 2 {
		if m.Content[i].Value != key {
			continue
		}
		m.Content = append(m.Content[:i], m.Content[i+2:]...)
		return true
	}
	return false
}

func (m *EditableYAML) DeleteListIf(
	predicate func(i int, item *EditableYAML) bool,
) error {
	if m == nil || m.Node == nil || m.Kind != yaml.SequenceNode {
		return fmt.Errorf("expected sequence node")
	}
	dst := m.Content[:0]
	for i, item := range m.Content {
		if predicate(i, WrapYAML(item)) {
			continue
		}

		dst = append(dst, item)
	}
	m.Content = dst
	return nil
}

func (m *EditableYAML) IterateList(iterFunc func(i int, item *EditableYAML) error) error {
	if m == nil || m.Node == nil || m.Kind != yaml.SequenceNode {
		return fmt.Errorf("expected sequence node")
	}
	for i, item := range m.Content {
		if err := iterFunc(i, WrapYAML(item)); err != nil {
			return fmt.Errorf("iteration failed at index %d: %w", i, err)
		}
	}
	return nil
}
